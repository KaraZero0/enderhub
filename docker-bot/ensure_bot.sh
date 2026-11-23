#!/usr/bin/env bash
set -euo pipefail

BOT_DIR="${BOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
LOCK_FILE="$BOT_DIR/state/bot.lock"
LOG_FILE="$BOT_DIR/bot.log"

mkdir -p "$BOT_DIR/state/stopped"

# Exit immediately if bot.py already holds the exclusive flock
if ! flock -n "$LOCK_FILE" true 2>/dev/null; then
    exit 0
fi

# Rotate log file above 2 MB
if [[ -f "$LOG_FILE" ]] && [[ $(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0) -gt 2097152 ]]; then
    mv -f "$LOG_FILE" "${LOG_FILE}.old"
fi

cd "$BOT_DIR"
nohup setsid python3 -u "$BOT_DIR/bot.py" </dev/null >>"$LOG_FILE" 2>&1 &
