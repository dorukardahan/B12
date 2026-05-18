#!/bin/bash
# B12 Memory System — FileChanged Hook (v1, 2026-05-18)
# Captures user edits to CLAUDE.md / MEMORY.md / .claude/rules/*.md
# as candidate memories.
#
# Fires on: FileChanged (literal-filename match — CLAUDE.md, MEMORY.md, rules)
# Output: empty JSON (side-effect only)
# Budget: <500ms, async
#
# Why: Convergent finding from Phase C research (Agent 1 + Agent 3).
# Native auto-memory (v2.1.59+) writes to ~/.claude/projects/<proj>/
# memory/MEMORY.md — the same MEMORY.md B12 also indexes. B12 today
# has no signal when the user (or Claude itself) edits a CLAUDE.md
# mid-session; rules drift silently between session-end snapshots.
# Watching disk-level FS events catches the edit immediately, opens
# a window to store the diff as a `learning` memory with
# `source: claude_md_edit`, and lets B12's spaced repetition treat
# self-authored rule changes as high-strength signals.
#
# Security caveat (anthropics/claude-code #44909): do NOT watch
# `.env` / credential paths — FileChanged will leak them. This hook's
# matcher is configured in settings-template.json to only fire on
# memory / instruction files, never secrets. Within the script we
# also defensively reject anything that looks like an env file.
#
# Anthropic caveat (anthropics/claude-code #44925): FileChanged does
# NOT fire when Claude's own Edit tool modifies a file. Those edits
# are already caught by PostToolUse(Edit). So this hook captures
# *out-of-band* edits: the user's editor, git operations, manual
# script writes — exactly the events B12 was missing.
#
# Scope limitation (Codex PR #40 round 4 catch): the FileChanged
# matcher field accepts LITERAL filenames only — `|`-split tokens are
# each registered as plain strings, not globs or regexes. So this
# hook ships with three universal watch names (CLAUDE.md,
# CLAUDE.local.md, MEMORY.md) and CANNOT watch `.claude/rules/*.md`
# via a single matcher entry. Per-rule watching requires the user to
# add each rule filename explicitly in their own settings.json;
# tracked as a P3 follow-up in
# docs/B12_claude_code_integration_audit_2026-05-18.md.

_B12_HOOK_DIR="${B12_HOOK_DIR:-$HOME/.B12/hooks}"
# shellcheck disable=SC1091
. "$_B12_HOOK_DIR/_b12_common.sh"

( sleep 4 && kill -TERM $$ 2>/dev/null ) &
_WATCHDOG=$!
trap "kill $_WATCHDOG 2>/dev/null; wait $_WATCHDOG 2>/dev/null" EXIT

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')
SESSION_ID12="${SESSION_ID:0:12}"
FILE_PATH=$(echo "$INPUT" | jq -r '.file_path // ""')
# FileChanged payload field naming varies across Claude Code versions:
# code.claude.com/docs/en/hooks documents `change_type` with values
# `created|modified|deleted`, but Codex review of PR #40 (2026-05-18)
# flagged that the actual on-the-wire field is `event` with values
# `add|change|unlink`. Read both — whichever is non-empty wins — and
# the case statement below accepts both value sets so this hook stays
# correct across versions without a config flip.
CHANGE_TYPE=$(echo "$INPUT" | jq -r '
  (.event // "") as $a
  | (.change_type // "") as $b
  | if ($a | length) > 0 then $a else $b end
')

# Bail-out gates.
if [ -z "$FILE_PATH" ]; then
  echo '{}'
  exit 0
fi

# Defensive secret-path reject. The matcher in settings should never
# send us .env, but if it ever does we drop on the floor.
case "$FILE_PATH" in
  *.env|*.env.*|*/.env|*/.env.*|*credentials*|*secrets*|*.pem|*.key)
    echo '{}'
    exit 0
    ;;
esac

# Only act on change events that imply new/updated content. Accept
# both value vocabularies — docs say `modified|created|deleted`, the
# actual wire format uses `change|add|unlink` (chokidar-ish names).
case "$CHANGE_TYPE" in
  modified|created|change|add) ;;
  *)
    echo '{}'
    exit 0
    ;;
esac

# Only act on plausible instruction/memory files. Belt-and-suspenders
# alongside the settings-template matcher.
case "$(basename "$FILE_PATH")" in
  CLAUDE.md|CLAUDE.local.md|MEMORY.md|*.md) ;;
  *)
    echo '{}'
    exit 0
    ;;
esac

# Skip files that no longer exist (race between event and read).
if [ ! -f "$FILE_PATH" ]; then
  echo '{}'
  exit 0
fi

# Skip files too large to be a rule / memory note (4 MB ceiling —
# rules and memory files are bytes-to-KB, not MB).
FILE_SIZE=$(wc -c < "$FILE_PATH" 2>/dev/null | tr -d ' ')
if [ -n "$FILE_SIZE" ] && [ "$FILE_SIZE" -gt 4194304 ]; then
  echo '{}'
  exit 0
fi

B12_BASE="${B12_DATA_DIR:-$HOME/.B12}"
STAGING_DIR="$B12_BASE/memory-staging/checkpoint"
mkdir -p "$STAGING_DIR" 2>/dev/null
BUFFER_FILE="$STAGING_DIR/.buffer-${SESSION_ID12}.jsonl"

# Async scan + buffer write. The Python heredoc reads the file,
# clips to the head 2 KB (rule files are typically front-loaded),
# and writes a single learning-category memory line to the existing
# checkpoint buffer. Dedup-via-content_hash inside the eventual
# flush keeps repeated edits from spamming the DB.
{
python3 - "$FILE_PATH" "$CHANGE_TYPE" "$BUFFER_FILE" "$_B12_HOOK_DIR/scripts" << 'PYEOF'
import sys, os, json, fcntl

file_path, change_type, buffer_file, scripts_dir = sys.argv[1:5]

sys.path.insert(0, scripts_dir)
try:
    from shared_patterns import content_hash, summary_filter
except ImportError:
    sys.exit(0)

try:
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        head = f.read(2048)
except OSError:
    sys.exit(0)

if not head.strip():
    sys.exit(0)

# Skip auto-generated MEMORY.md index lines — those are Claude's own
# scratch pad maintenance, not user-authored rule changes.
if summary_filter(head):
    sys.exit(0)

# Mark source so future retrieval can filter or upweight user edits
# vs Claude-auto-memory writes.
basename = os.path.basename(file_path)
content = f"[user_edit:{basename}:{change_type}] {head.strip()[:600]}"

h = content_hash(content[:200])

lock_path = buffer_file + ".lock"
try:
    lock_fh = open(lock_path, "a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
    except OSError:
        pass
    with open(buffer_file, "a") as f:
        f.write(json.dumps({
            "content": content[:500],
            "category": "learning",
            "score": 7,
            "hash": h,
            "source": f"file_changed:{basename}",
        }, ensure_ascii=False) + "\n")
except OSError:
    pass
PYEOF
} >/dev/null 2>&1 &
disown

echo '{}'
exit 0
