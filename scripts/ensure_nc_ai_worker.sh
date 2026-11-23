#!/usr/bin/env bash
set -euo pipefail

SCRIPTS_DIR="${ENDERHUB_SCRIPTS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
"$SCRIPTS_DIR/self_heal_ports.sh"

# Keep Nextcloud Assistant real-time background worker running ONLY when nextcloud_app is up
if docker inspect -f '{{.State.Running}}' nextcloud_app 2>/dev/null | grep -qx 'true'; then
    if ! docker exec nextcloud_app pgrep -f "SynchronousBackgroundJob" >/dev/null 2>&1; then
        docker exec -d -u www-data nextcloud_app sh -c "while true; do php /var/www/html/occ background-job:worker -t 3600 \"OC\\TaskProcessing\\SynchronousBackgroundJob\" >/dev/null 2>&1; sleep 1; done"
    fi
fi
