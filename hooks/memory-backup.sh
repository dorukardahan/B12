#!/bin/bash
# B12 Memory System - Daily DB Backup (v1)
# Creates rotated backups of the memory SQLite database
#
# Usage: bash ~/.B12/hooks/memory-backup.sh
# Schedule: Daily 1:00 AM via launchd (com.b12.memory-backup)
#
# Keeps last 7 backups. Uses sqlite3 .backup for WAL-safe copies.

set -o pipefail 2>/dev/null || true

# shellcheck source=./_b12_common.sh disable=SC1091
. "${B12_HOOK_DIR:-$HOME/.B12/hooks}/_b12_common.sh" 2>/dev/null || true
if command -v b12_resolve_db_path >/dev/null 2>&1; then
  DB_PATH="$(b12_resolve_db_path)"
else
  if [ "$(uname)" = "Darwin" ]; then
    DB_PATH="$HOME/Library/Application Support/mcp-memory/sqlite_vec.db"
  elif [ -d "$HOME/AppData" ]; then
    DB_PATH="$HOME/AppData/Local/mcp-memory/sqlite_vec.db"
  else
    DB_PATH="$HOME/.local/share/mcp-memory/sqlite_vec.db"
  fi
fi
BACKUP_DIR="${B12_DATA_DIR:-$HOME/.B12}/memory-backups"
MAX_BACKUPS=7

# Portable stat: macOS uses -f%z, Linux uses -c %s
file_size() { stat -f%z "$1" 2>/dev/null || stat -c %s "$1" 2>/dev/null || echo "unknown"; }
TIMESTAMP=$(date +"%Y-%m-%d")
BACKUP_FILE="$BACKUP_DIR/sqlite_vec-${TIMESTAMP}.db"
LOG_FILE="${B12_DATA_DIR:-$HOME/.B12}/memory-logs/backup-${TIMESTAMP}.log"

mkdir -p "$BACKUP_DIR" "${B12_DATA_DIR:-$HOME/.B12}/memory-logs" 2>/dev/null

log() { echo "[$(date +%H:%M:%S)] $1" | tee -a "$LOG_FILE"; }

# Check source exists
if [ ! -f "$DB_PATH" ]; then
  log "ERROR: DB not found at $DB_PATH"
  exit 1
fi

# Skip if today's backup already exists
if [ -f "$BACKUP_FILE" ]; then
  log "Backup already exists for today: $BACKUP_FILE"
  exit 0
fi

# Create backup using sqlite3 .backup (WAL-safe)
log "Starting backup..."
DB_SIZE=$(file_size "$DB_PATH")
log "Source: $DB_PATH ($DB_SIZE bytes)"

# Flush WAL to main DB before backup (ensures clean copy)
sqlite3 "$DB_PATH" "PRAGMA wal_checkpoint(TRUNCATE);" 2>>"$LOG_FILE"
log "WAL checkpoint completed"

if sqlite3 "$DB_PATH" ".backup '$BACKUP_FILE'" 2>>"$LOG_FILE"; then
  BACKUP_SIZE=$(file_size "$BACKUP_FILE")
  log "Backup created: $BACKUP_FILE ($BACKUP_SIZE bytes)"
else
  log "ERROR: sqlite3 .backup failed"
  rm -f "$BACKUP_FILE"
  exit 1
fi

# Verify backup integrity
if sqlite3 "$BACKUP_FILE" "PRAGMA integrity_check;" 2>/dev/null | grep -q "ok"; then
  log "Integrity check: OK"
else
  log "WARNING: Integrity check failed on backup"
fi

# Count memories in backup for verification
MEM_COUNT=$(sqlite3 "$BACKUP_FILE" "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL;" 2>/dev/null || echo "?")
log "Memories in backup: $MEM_COUNT"

# Rotate old backups (keep last MAX_BACKUPS)
BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/sqlite_vec-*.db 2>/dev/null | wc -l | tr -d ' ')
if [ "$BACKUP_COUNT" -gt "$MAX_BACKUPS" ]; then
  REMOVE_COUNT=$((BACKUP_COUNT - MAX_BACKUPS))
  ls -1t "$BACKUP_DIR"/sqlite_vec-*.db | tail -n "$REMOVE_COUNT" | while read -r old; do
    log "Rotating old backup: $(basename "$old")"
    rm -f "$old"
  done
fi

# Clean old log files (keep last 14)
B12_LOG_DIR="${B12_DATA_DIR:-$HOME/.B12}/memory-logs"
LOG_COUNT=$(ls -1 "$B12_LOG_DIR"/backup-*.log 2>/dev/null | wc -l | tr -d ' ')
if [ "$LOG_COUNT" -gt 14 ]; then
  ls -1t "$B12_LOG_DIR"/backup-*.log | tail -n $((LOG_COUNT - 14)) | while read -r old; do
    rm -f "$old"
  done
fi

log "Done. Backups retained: $(ls -1 "$BACKUP_DIR"/sqlite_vec-*.db 2>/dev/null | wc -l | tr -d ' ')/$MAX_BACKUPS"
