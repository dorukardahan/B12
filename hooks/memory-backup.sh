#!/bin/bash
# B12 Memory System - Daily DB Backup (v1)
# Creates rotated backups of the memory SQLite database
#
# Usage: bash ~/.claude/hooks/memory-backup.sh
# Schedule: Daily 1:00 AM via launchd (com.b12.memory-backup)
#
# Keeps last 7 backups. Uses sqlite3 .backup for WAL-safe copies.

DB_PATH="$HOME/Library/Application Support/mcp-memory/sqlite_vec.db"
BACKUP_DIR="$HOME/.claude/memory-backups"
MAX_BACKUPS=7

# Portable stat: macOS uses -f%z, Linux uses -c %s
file_size() { stat -f%z "$1" 2>/dev/null || stat -c %s "$1" 2>/dev/null || echo "unknown"; }
TIMESTAMP=$(date +"%Y-%m-%d")
BACKUP_FILE="$BACKUP_DIR/sqlite_vec-${TIMESTAMP}.db"
LOG_FILE="$HOME/.claude/memory-logs/backup-${TIMESTAMP}.log"

mkdir -p "$BACKUP_DIR" "$HOME/.claude/memory-logs" 2>/dev/null

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
LOG_COUNT=$(ls -1 "$HOME/.claude/memory-logs"/backup-*.log 2>/dev/null | wc -l | tr -d ' ')
if [ "$LOG_COUNT" -gt 14 ]; then
  ls -1t "$HOME/.claude/memory-logs"/backup-*.log | tail -n $((LOG_COUNT - 14)) | while read -r old; do
    rm -f "$old"
  done
fi

log "Done. Backups retained: $(ls -1 "$BACKUP_DIR"/sqlite_vec-*.db 2>/dev/null | wc -l | tr -d ' ')/$MAX_BACKUPS"
