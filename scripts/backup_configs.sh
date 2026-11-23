#!/usr/bin/env bash
set -euo pipefail

BASE_HOME="${ENDERHUB_HOME:-/home/server}"
NEXTCLOUD_DIR="${NEXTCLOUD_DIR:-/mnt/nextcloud}"
BACKUP_DIR="${BACKUP_DIR:-$BASE_HOME/backups}"
STAGE_DIR=$(mktemp -d)
trap 'rm -rf "$STAGE_DIR"' EXIT

mkdir -p "$BACKUP_DIR" "$STAGE_DIR/nextcloud" "$STAGE_DIR/monitoraggio" "$STAGE_DIR/ai-stack" "$STAGE_DIR/pairdrop" "$STAGE_DIR/grass" "$STAGE_DIR/bitping" "$STAGE_DIR/system"

# 1. Nextcloud config & hot MariaDB SQL dump
docker exec nextcloud_app cat /var/www/html/config/config.php > "$STAGE_DIR/nextcloud/config.php" 2>/dev/null || true
cp "$NEXTCLOUD_DIR/docker-compose.yml" "$STAGE_DIR/nextcloud/docker-compose.yml" 2>/dev/null || true
cp "$NEXTCLOUD_DIR/.env" "$STAGE_DIR/nextcloud/.env" 2>/dev/null || true
DB_PASS=$(docker exec nextcloud_db printenv MYSQL_ROOT_PASSWORD 2>/dev/null || true)
if [[ -n "$DB_PASS" ]]; then
    docker exec nextcloud_db mariadb-dump -u root -p"$DB_PASS" --single-transaction --routines --triggers --all-databases 2>/dev/null \
      | gzip > "$STAGE_DIR/nextcloud/nextcloud_db_dump.sql.gz" || true
fi

# 2. Monitoring & AI Stack (Ollama + Open-WebUI SQLite DB)
cp "$BASE_HOME/monitoraggio/docker-compose.yml" "$STAGE_DIR/monitoraggio/" 2>/dev/null || true
cp -r "$BASE_HOME/monitoraggio/prometheus" "$STAGE_DIR/monitoraggio/" 2>/dev/null || true
cp -r "$BASE_HOME/monitoraggio/provisioning" "$STAGE_DIR/monitoraggio/" 2>/dev/null || true
cp "$BASE_HOME/ai-stack/docker-compose.yml" "$STAGE_DIR/ai-stack/" 2>/dev/null || true
cp "$BASE_HOME/ai-stack/.env" "$STAGE_DIR/ai-stack/" 2>/dev/null || true
docker exec open-webui cat /app/backend/data/webui.db > "$STAGE_DIR/ai-stack/webui.db" 2>/dev/null || true

# 3. Utility & Node Compose files
cp "$BASE_HOME/pairdrop/docker-compose.yml" "$STAGE_DIR/pairdrop/" 2>/dev/null || true
cp "$BASE_HOME/grass/docker-compose.yml" "$STAGE_DIR/grass/" 2>/dev/null || true
cp "$BASE_HOME/bitping/docker-compose.yml" "$STAGE_DIR/bitping/" 2>/dev/null || true

# 4. Scripts, Bot & System configs
cp -r "$BASE_HOME/scripts" "$STAGE_DIR/" 2>/dev/null || true
rsync -a --exclude='bot.log*' --exclude='state/bot.lock' --exclude='__pycache__' \
  "$BASE_HOME/docker-bot/" "$STAGE_DIR/docker-bot/" 2>/dev/null || true
crontab -l > "$STAGE_DIR/system/crontab.txt" 2>/dev/null || true
cp /etc/fstab "$STAGE_DIR/system/fstab" 2>/dev/null || true
cp /etc/default/grub "$STAGE_DIR/system/grub_default" 2>/dev/null || true
cp -r /etc/systemd/system/grub-common.service.d "$STAGE_DIR/system/" 2>/dev/null || true
cp /etc/fail2ban/jail.local "$STAGE_DIR/system/jail.local" 2>/dev/null || true

TS="$(date +%Y%m%d_%H%M%S)"
ARCHIVE="$BACKUP_DIR/configs_enderhub_${TS}.tar.gz"
LATEST="$BACKUP_DIR/configs_enderhub_latest.tar.gz"
TMP_LATEST="$BACKUP_DIR/.configs_enderhub_latest.tar.gz.tmp"

tar -czf "$ARCHIVE" -C "$STAGE_DIR" .
chmod 600 "$ARCHIVE"
cp -f "$ARCHIVE" "$TMP_LATEST"
chmod 600 "$TMP_LATEST"
mv -f "$TMP_LATEST" "$LATEST"

# Retain last 7 snapshots
ls -1t "$BACKUP_DIR"/configs_enderhub_2*.tar.gz 2>/dev/null | tail -n +8 | xargs -r rm -f
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Backup completed: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
