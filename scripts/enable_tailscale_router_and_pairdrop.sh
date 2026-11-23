#!/usr/bin/env bash
set -euo pipefail

LAN_SUBNET="${ENDERHUB_LAN_SUBNET:-192.168.1.0/24}"
NET_IFACE="${ENDERHUB_NET_IFACE:-eth0}"

echo "[1/3] Enabling Tailscale Exit Node & Subnet Router ($LAN_SUBNET)..."
tailscale set --advertise-exit-node --advertise-routes="$LAN_SUBNET" --snat-subnet-routes=true

if command -v ethtool >/dev/null 2>&1; then
    ethtool -K "$NET_IFACE" rx-udp-gro-forwarding on rx-gro-list off 2>/dev/null || true
    if [[ -d /etc/networkd-dispatcher/routable.d ]]; then
        cat << EOF > /etc/networkd-dispatcher/routable.d/50-tailscale-gro
#!/bin/sh
ethtool -K ${NET_IFACE} rx-udp-gro-forwarding on rx-gro-list off 2>/dev/null || true
EOF
        chmod 755 /etc/networkd-dispatcher/routable.d/50-tailscale-gro
    fi
fi

echo "[2/3] Allowing PairDrop (3002/tcp) on LAN and Tailscale forwarding..."
ufw allow from "$LAN_SUBNET" to any port 3002 proto tcp comment 'PairDrop from LAN'
ufw route allow in on tailscale0 out on "$NET_IFACE" comment 'Tailscale Exit Node & Subnet Router'
ufw reload

echo "[3/3] Tailscale status:"
tailscale status
