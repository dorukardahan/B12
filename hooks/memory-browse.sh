#!/bin/bash
# B12 Memory System - Memory Browser CLI (v1)
# Browse, search, and manage memories from terminal
#
# Usage:
#   memory-browse.sh                    # List all memories
#   memory-browse.sh list               # List all memories
#   memory-browse.sh search <query>     # FTS5 keyword search
#   memory-browse.sh stats              # DB statistics
#   memory-browse.sh show <hash>        # Show full memory by hash prefix
#   memory-browse.sh delete <hash>      # Soft-delete a memory
#   memory-browse.sh types              # List by memory type
#   memory-browse.sh tags               # List all tags with counts
#   memory-browse.sh project <name>     # List memories for a project

DB_PATH="$HOME/Library/Application Support/mcp-memory/sqlite_vec.db"

if [ ! -f "$DB_PATH" ]; then
  echo "Error: DB not found at $DB_PATH"
  exit 1
fi

# Colors
BOLD="\033[1m"
DIM="\033[2m"
CYAN="\033[36m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

CMD="${1:-list}"

case "$CMD" in
  list)
    echo -e "${BOLD}Memory List${RESET} (active, newest first)\n"
    sqlite3 -separator '|' "$DB_PATH" "
      SELECT substr(content_hash, 1, 8), memory_type,
             datetime(created_at, 'unixepoch', 'localtime'),
             replace(replace(substr(content, 1, 80), char(10), ' '), char(13), ''),
             tags
      FROM memories
      WHERE deleted_at IS NULL
      ORDER BY created_at DESC
    " | while IFS='|' read -r hash type created preview tags; do
      echo -e "${CYAN}${hash}${RESET} ${YELLOW}[${type}]${RESET} ${DIM}${created}${RESET}"
      echo -e "  ${preview}..."
      [ -n "$tags" ] && echo -e "  ${DIM}tags: ${tags}${RESET}"
      echo ""
    done
    ;;

  search)
    QUERY="${2:-}"
    if [ -z "$QUERY" ]; then
      echo "Usage: memory-browse.sh search <query>"
      exit 1
    fi
    # Build FTS5 query (OR between words), sanitize SQL-dangerous chars
    SAFE_QUERY=$(echo "$QUERY" | sed "s/['\";(){}]//g")
    FTS_QUERY=$(echo "$SAFE_QUERY" | tr ' ' '\n' | awk 'length > 1 {printf "\"%s\" OR ", $0}' | sed 's/ OR $//')

    echo -e "${BOLD}Search: ${QUERY}${RESET} (FTS5: ${FTS_QUERY})\n"
    sqlite3 -separator '|' "$DB_PATH" "
      SELECT substr(m.content_hash, 1, 8), m.memory_type,
             datetime(m.created_at, 'unixepoch', 'localtime'),
             replace(replace(substr(m.content, 1, 120), char(10), ' '), char(13), ''),
             printf('%.2f', f.rank)
      FROM memory_fts f
      JOIN memories m ON m.id = f.rowid
      WHERE memory_fts MATCH '${FTS_QUERY}'
        AND m.deleted_at IS NULL
      ORDER BY f.rank
      LIMIT 10
    " | while IFS='|' read -r hash type created preview rank; do
      echo -e "${CYAN}${hash}${RESET} ${YELLOW}[${type}]${RESET} ${DIM}${created}${RESET} rank=${rank}"
      echo -e "  ${preview}..."
      echo ""
    done
    ;;

  stats)
    echo -e "${BOLD}Memory System Stats${RESET}\n"

    TOTAL=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL")
    DELETED=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memories WHERE deleted_at IS NOT NULL")
    EMBEDDINGS=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memory_embeddings" 2>/dev/null || echo "N/A (vec0)")
    FTS_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memory_fts" 2>/dev/null || echo "N/A")
    GRAPH_EDGES=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM memory_graph" 2>/dev/null || echo "0")
    DB_SIZE=$(stat -f%z "$DB_PATH" 2>/dev/null || echo "?")

    echo -e "  Active memories:  ${GREEN}${TOTAL}${RESET}"
    echo -e "  Deleted (tombstones): ${DIM}${DELETED}${RESET}"
    echo -e "  Embeddings:       ${EMBEDDINGS}"
    echo -e "  FTS5 indexed:     ${FTS_COUNT}"
    echo -e "  Graph edges:      ${GRAPH_EDGES}"
    echo -e "  DB size:          $(echo "$DB_SIZE" | awk '{printf "%.1f MB", $1/1048576}')"
    echo ""

    echo -e "${BOLD}By Type:${RESET}"
    sqlite3 "$DB_PATH" "
      SELECT '  ' || memory_type || ': ' || COUNT(*)
      FROM memories WHERE deleted_at IS NULL
      GROUP BY memory_type ORDER BY COUNT(*) DESC
    "
    echo ""

    echo -e "${BOLD}By Project:${RESET}"
    sqlite3 "$DB_PATH" "
      SELECT tags FROM memories WHERE deleted_at IS NULL
    " | grep -oE 'proj:[a-zA-Z0-9_-]+' | sort | uniq -c | sort -rn | head -10 | while read -r count tag; do
      echo "  ${tag}: ${count}"
    done

    echo ""
    BACKUP_COUNT=$(ls -1 "$HOME/.claude/memory-backups"/sqlite_vec-*.db 2>/dev/null | wc -l | tr -d ' ')
    LATEST_BACKUP=$(ls -1t "$HOME/.claude/memory-backups"/sqlite_vec-*.db 2>/dev/null | head -1)
    if [ -n "$LATEST_BACKUP" ]; then
      echo -e "${BOLD}Backups:${RESET} ${BACKUP_COUNT} (latest: $(basename "$LATEST_BACKUP"))"
    else
      echo -e "${BOLD}Backups:${RESET} ${RED}none${RESET}"
    fi
    ;;

  show)
    HASH_PREFIX="${2:-}"
    if [ -z "$HASH_PREFIX" ]; then
      echo "Usage: memory-browse.sh show <hash-prefix>"
      exit 1
    fi

    # Sanitize: hash prefixes should be hex only
    HASH_PREFIX=$(echo "$HASH_PREFIX" | sed "s/[^a-fA-F0-9]//g")

    # Use separate queries to avoid multiline pipe parsing issues
    HEADER=$(sqlite3 -separator '|' "$DB_PATH" "
      SELECT content_hash, memory_type, tags, metadata,
             datetime(created_at, 'unixepoch', 'localtime')
      FROM memories
      WHERE content_hash LIKE '${HASH_PREFIX}%'
        AND deleted_at IS NULL
      LIMIT 1
    ")

    if [ -z "$HEADER" ]; then
      echo "No memory found with hash prefix: ${HASH_PREFIX}"
      exit 1
    fi

    IFS='|' read -r hash type tags meta created <<< "$HEADER"

    echo -e "${BOLD}Memory: ${CYAN}${hash}${RESET}"
    echo -e "Type: ${YELLOW}${type}${RESET}"
    echo -e "Tags: ${tags}"
    echo -e "Created: ${created}"
    echo -e "Metadata: ${meta}"
    echo -e "\n${BOLD}Content:${RESET}"
    sqlite3 "$DB_PATH" "
      SELECT content FROM memories
      WHERE content_hash LIKE '${HASH_PREFIX}%'
        AND deleted_at IS NULL
      LIMIT 1
    "
    ;;

  delete)
    HASH_PREFIX="${2:-}"
    if [ -z "$HASH_PREFIX" ]; then
      echo "Usage: memory-browse.sh delete <hash-prefix>"
      exit 1
    fi

    # Sanitize: hash prefixes should be hex only
    HASH_PREFIX=$(echo "$HASH_PREFIX" | sed "s/[^a-fA-F0-9]//g")

    # Show what will be deleted
    PREVIEW=$(sqlite3 "$DB_PATH" "
      SELECT substr(content_hash, 1, 16) || ' [' || memory_type || '] ' ||
             replace(replace(substr(content, 1, 80), char(10), ' '), char(13), '')
      FROM memories
      WHERE content_hash LIKE '${HASH_PREFIX}%' AND deleted_at IS NULL
    ")

    if [ -z "$PREVIEW" ]; then
      echo "No active memory found with hash prefix: ${HASH_PREFIX}"
      exit 1
    fi

    echo -e "${RED}Will soft-delete:${RESET}"
    echo "  $PREVIEW"
    echo ""
    read -p "Confirm? [y/N] " CONFIRM

    if [ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ]; then
      sqlite3 "$DB_PATH" "
        UPDATE memories SET deleted_at = unixepoch()
        WHERE content_hash LIKE '${HASH_PREFIX}%' AND deleted_at IS NULL
      "
      echo -e "${GREEN}Soft-deleted.${RESET} (Use memory_cleanup to permanently remove)"
    else
      echo "Cancelled."
    fi
    ;;

  types)
    echo -e "${BOLD}Memories by Type${RESET}\n"
    sqlite3 -separator '|' "$DB_PATH" "
      SELECT memory_type, COUNT(*), GROUP_CONCAT(substr(content_hash, 1, 8), ', ')
      FROM memories WHERE deleted_at IS NULL
      GROUP BY memory_type ORDER BY COUNT(*) DESC
    " | while IFS='|' read -r type count hashes; do
      echo -e "${YELLOW}${type}${RESET} (${count})"
      echo -e "  ${DIM}hashes: ${hashes}${RESET}"
    done
    ;;

  tags)
    echo -e "${BOLD}Tag Distribution${RESET}\n"
    sqlite3 "$DB_PATH" "
      SELECT tags FROM memories WHERE deleted_at IS NULL
    " | tr ',' '\n' | sed 's/^ *//' | sort | uniq -c | sort -rn | while read -r count tag; do
      echo -e "  ${count}x ${CYAN}${tag}${RESET}"
    done
    ;;

  project)
    PROJ="${2:-}"
    if [ -z "$PROJ" ]; then
      echo "Usage: memory-browse.sh project <name>"
      exit 1
    fi

    # Sanitize: project names should be alphanumeric/dash/underscore
    PROJ=$(echo "$PROJ" | sed "s/[^a-zA-Z0-9_.-]//g")

    echo -e "${BOLD}Memories for project: ${PROJ}${RESET}\n"
    sqlite3 -separator '|' "$DB_PATH" "
      SELECT substr(content_hash, 1, 8), memory_type,
             datetime(created_at, 'unixepoch', 'localtime'),
             replace(replace(substr(content, 1, 100), char(10), ' '), char(13), '')
      FROM memories
      WHERE tags LIKE '%proj:${PROJ}%'
        AND deleted_at IS NULL
      ORDER BY created_at DESC
    " | while IFS='|' read -r hash type created preview; do
      echo -e "${CYAN}${hash}${RESET} ${YELLOW}[${type}]${RESET} ${DIM}${created}${RESET}"
      echo -e "  ${preview}..."
      echo ""
    done
    ;;

  *)
    echo "B12 Memory Browser"
    echo ""
    echo "Commands:"
    echo "  list                List all memories"
    echo "  search <query>      FTS5 keyword search"
    echo "  stats               DB statistics"
    echo "  show <hash>         Show full memory content"
    echo "  delete <hash>       Soft-delete a memory"
    echo "  types               Group by memory type"
    echo "  tags                Tag distribution"
    echo "  project <name>      Filter by project"
    ;;
esac
