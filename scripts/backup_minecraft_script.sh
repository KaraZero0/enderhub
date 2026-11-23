#!/usr/bin/env bash
set -euo pipefail

BASE_HOME="${ENDERHUB_HOME:-/home/server}"
MINECRAFT_DIR="${MINECRAFT_DIR:-$BASE_HOME/minecraft-server}"
BACKUP_DIR="${BACKUP_DIR:-$BASE_HOME/backups}"
mkdir -p "$BACKUP_DIR"

BACKUP_FILE="$BACKUP_DIR/latest_minecraft_server_backup.tar.gz"

# Skip compression if no files changed since last backup
if [[ -f "$BACKUP_FILE" ]]; then
    CHANGED_FILES=$(find "$MINECRAFT_DIR" -newer "$BACKUP_FILE" -print -quit 2>/dev/null || true)
    if [[ -z "$CHANGED_FILES" ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] No changes in $MINECRAFT_DIR. Skipping."
        exit 0
    fi
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Stopping container & creating backup..."
(cd "$MINECRAFT_DIR" && docker compose down)

TEMP_BACKUP="$BACKUP_DIR/latest_minecraft_server_backup.tmp.tar.gz"
tar -czf "$TEMP_BACKUP" "$MINECRAFT_DIR"
mv -f "$TEMP_BACKUP" "$BACKUP_FILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Backup completed: $BACKUP_FILE"
