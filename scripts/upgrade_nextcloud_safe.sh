#!/usr/bin/env bash
set -euo pipefail

# Sequential Nextcloud major-version migration helper (e.g. 31 -> 32 -> 33 -> 34 -> 35)
BASE_HOME="${ENDERHUB_HOME:-/home/server}"
NEXTCLOUD_DIR="${NEXTCLOUD_DIR:-/mnt/nextcloud}"
LAN_IP="${ENDERHUB_LAN_IP:-192.168.1.100}"
TAILSCALE_IP="${ENDERHUB_TAILSCALE_IP:-100.x.y.z}"
ENV_DUMP="$BASE_HOME/.nc_compose_env"

if [[ ! -f "$ENV_DUMP" ]]; then
    docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' nextcloud_app \
      | grep -E '^(MYSQL_|REDIS_|OVERWRITE|TRUSTED_)' > "$ENV_DUMP"
    chmod 600 "$ENV_DUMP"
fi

upgrade_to_version() {
    local TAG="$1"
    echo ">>> Upgrading to nextcloud:${TAG}"
    docker pull "nextcloud:${TAG}"
    docker stop nextcloud_app
    docker rm nextcloud_app

    docker run -d \
      --name nextcloud_app \
      --restart unless-stopped \
      --network nextcloud_nextcloud_network \
      --network-alias app \
      --env-file "$ENV_DUMP" \
      -p "${LAN_IP}:80:80" \
      -p "${TAILSCALE_IP}:80:80" \
      -p "127.0.0.1:80:80" \
      --add-host enderhub:127.0.0.1 \
      -v "$NEXTCLOUD_DIR/nextcloud_data:/var/www/html" \
      --label com.docker.compose.project=nextcloud \
      --label com.docker.compose.service=app \
      "nextcloud:${TAG}"

    for _ in $(seq 1 90); do
        if docker logs nextcloud_app 2>&1 | grep -q "resuming normal operations"; then
            break
        fi
        sleep 2
    done

    docker exec -u www-data nextcloud_app php occ maintenance:mode --off || true
    docker exec -u www-data nextcloud_app php occ upgrade || true
    docker exec -u www-data nextcloud_app php occ db:add-missing-indices || true
    docker exec -u www-data nextcloud_app php occ db:add-missing-columns || true
    docker exec -u www-data nextcloud_app php occ db:add-missing-primary-keys || true
    docker exec -u www-data nextcloud_app php occ app:update --all || true
}

for ver in "32-apache" "33-apache" "34-apache" "35-apache"; do
    upgrade_to_version "$ver"
done

docker tag nextcloud:35-apache nextcloud:apache
echo "✅ Sequential Nextcloud upgrade complete."
