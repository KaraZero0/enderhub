#!/usr/bin/env bash
# Self-healing watchdog: restores Docker bindings on Tailscale IP after unclean reboots,
# while respecting stacks intentionally stopped via the Telegram bot.
set -euo pipefail

BASE_HOME="${ENDERHUB_HOME:-/home/server}"
NEXTCLOUD_DIR="${NEXTCLOUD_DIR:-/mnt/nextcloud}"
STOPPED_DIR="${STOPPED_DIR:-$BASE_HOME/docker-bot/state/stopped}"
TAILSCALE_IP="${ENDERHUB_TAILSCALE_IP:-100.x.y.z}"

ip addr show dev tailscale0 2>/dev/null | grep -q "$TAILSCALE_IP" || exit 0
LISTENING=$(ss -tln 2>/dev/null || true)

heal_stack() {
    local id="$1"
    local port="$2"
    local dir="$3"
    [[ -f "$STOPPED_DIR/$id" ]] && return 0
    if ! grep -qE "$TAILSCALE_IP:$port([[:space:]]|$)" <<< "$LISTENING"; then
        cd "$dir" && docker compose up -d >/dev/null 2>&1 || true
    fi
}

heal_stack "nextcloud" "80" "$NEXTCLOUD_DIR"
heal_stack "ai_stack" "3001" "$BASE_HOME/ai-stack"
heal_stack "pairdrop" "3002" "$BASE_HOME/pairdrop"
heal_stack "monitoraggio" "3000" "$BASE_HOME/monitoraggio"
heal_stack "grass" "5800" "$BASE_HOME/grass"
