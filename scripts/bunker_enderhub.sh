#!/usr/bin/env bash
set -euo pipefail

# Parameterized Bunker-Mode hardening script for Ubuntu Server + Docker + Tailscale
# Override these env vars before running with sudo -E bash bunker_enderhub.sh
LAN_IP="${ENDERHUB_LAN_IP:-192.168.1.100}"
LAN_SUBNET="${ENDERHUB_LAN_SUBNET:-192.168.1.0/24}"
TAILSCALE_IP="${ENDERHUB_TAILSCALE_IP:-100.x.y.z}"
CLIENT_TAILSCALE_IP="${CLIENT_TAILSCALE_IP:-100.a.b.c}"
SSH_PORT="${ENDERHUB_SSH_PORT:-2222}"
NEXTCLOUD_DIR="${NEXTCLOUD_DIR:-/mnt/nextcloud}"

if [[ $EUID -ne 0 ]]; then
   echo "Error: run with sudo -E bash $0"
   exit 1
fi

echo "[1/4] Isolating Nextcloud bindings to LAN & Tailscale (no public IPv6/0.0.0.0)..."
chmod 600 "$NEXTCLOUD_DIR/.env"
cat << EOF > "$NEXTCLOUD_DIR/docker-compose.yml"
services:
  db:
    image: mariadb:11
    container_name: nextcloud_db
    restart: unless-stopped
    environment:
      MYSQL_ROOT_PASSWORD: \${MYSQL_ROOT_PASSWORD}
      MYSQL_DATABASE: \${MYSQL_DATABASE}
      MYSQL_USER: \${MYSQL_USER}
      MYSQL_PASSWORD: \${MYSQL_PASSWORD}
    volumes:
      - ./nextcloud_db_data:/var/lib/mysql
    networks:
      - nextcloud_network

  redis:
    image: redis:alpine
    container_name: nextcloud_redis
    restart: unless-stopped
    networks:
      - nextcloud_network

  app:
    image: nextcloud:apache
    container_name: nextcloud_app
    restart: unless-stopped
    ports:
      - "${LAN_IP}:80:80"
      - "${TAILSCALE_IP}:80:80"
      - "127.0.0.1:80:80"
    environment:
      MYSQL_HOST: db
      MYSQL_DATABASE: \${MYSQL_DATABASE}
      MYSQL_USER: \${MYSQL_USER}
      MYSQL_PASSWORD: \${MYSQL_PASSWORD}
      REDIS_HOST: redis
      OVERWRITEPROTOCOL: http
      OVERWRITEHOST: "enderhub"
      TRUSTED_PROXIES: "${LAN_IP} ${TAILSCALE_IP}"
    volumes:
      - ./nextcloud_data:/var/www/html
    depends_on:
      - db
      - redis
    extra_hosts:
      - "enderhub:127.0.0.1"
    networks:
      - nextcloud_network

networks:
  nextcloud_network:
    driver: bridge
EOF
chmod 600 "$NEXTCLOUD_DIR/docker-compose.yml"
(cd "$NEXTCLOUD_DIR" && docker compose up -d)

echo "[2/4] Configuring Fail2ban (monitoring LAN as well)..."
[[ -f /etc/fail2ban/jail.local ]] && cp /etc/fail2ban/jail.local "/etc/fail2ban/jail.local.bak.$(date +%Y%m%d_%H%M%S)"
cat << EOF > /etc/fail2ban/jail.local
[DEFAULT]
ignoreip = 127.0.0.1/8 ::1 ${TAILSCALE_IP} ${CLIENT_TAILSCALE_IP}
bantime  = 3600
findtime = 600
maxretry = 4
banaction = nftables
banaction_allports = nftables[type=allports]
backend = systemd

[sshd]
enabled = true
port    = ${SSH_PORT}

[recidive]
enabled  = true
bantime  = 31536000
findtime = 604800
maxretry = 4
EOF
systemctl restart fail2ban

echo "[3/4] Configuring UFW firewall (Tailscale + LAN IPv4 only)..."
ufw default deny incoming
ufw default allow outgoing
ufw allow in on tailscale0 comment 'Tailscale VPN Trusted'
ufw allow 41641/udp comment 'Tailscale Direct WireGuard'
ufw allow from "$LAN_SUBNET" to any port "$SSH_PORT" proto tcp comment 'SSH from LAN'
ufw allow from "$LAN_SUBNET" to any port 80 proto tcp comment 'Nextcloud from LAN'
ufw allow from "$LAN_SUBNET" to any port 3000 proto tcp comment 'Grafana from LAN'
ufw allow from "$LAN_SUBNET" to any port 3001 proto tcp comment 'Open-WebUI from LAN'
ufw --force enable
ufw reload

echo "[4/4] Applying kernel network hardening (/etc/sysctl.d/99-bunker-security.conf)..."
cat << 'EOF' > /etc/sysctl.d/99-bunker-security.conf
net.ipv4.tcp_syncookies = 1
net.ipv4.tcp_max_syn_backlog = 2048
net.ipv4.tcp_synack_retries = 2

net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.secure_redirects = 0
net.ipv4.conf.default.secure_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0

net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0
net.ipv6.conf.all.accept_source_route = 0
net.ipv6.conf.default.accept_source_route = 0

kernel.kptr_restrict = 2
kernel.dmesg_restrict = 1
EOF
sysctl --system >/dev/null

echo "✅ Bunker Mode active."
ufw status numbered
