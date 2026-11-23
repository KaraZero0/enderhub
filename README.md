🇬🇧 [Read in English](#-english) | 🇮🇹 [Leggi in Italiano](#-italiano)

---

# 🇬🇧 English

# EnderHub — Fault-Tolerant Linux Home Server, Bunker Security & AI Stack

Reference architecture and automation toolkit for a 24/7 bare-metal **Ubuntu 24.04 LTS** home server running **LUKS2 + TPM 2.0**, **LVM2**, **Tailscale WireGuard mesh**, **Docker Compose**, **Prometheus/Grafana observability**, **local GPU LLM inference (Ollama + Open-WebUI)**, and a **zero-dependency Telegram management bot**.

> [!NOTE]
> All IP addresses, disk serials, UUIDs, user IDs, and credentials have been removed or parameterized via environment variables.

## 🏗️ Architecture Highlights

1. **Encrypted & Resilient Storage (LUKS2 + TPM 2.0 + LVM2)**
   - Root filesystem `/` protected by **LUKS2** full-disk encryption with automatic **TPM 2.0 (`clevis`)** unlock at boot.
   - Multi-SSD **LVM2** data volume (`/mnt`) dedicated to Nextcloud + MariaDB + Redis.
   - **Power-Outage Shield**: `GRUB_RECORDFAIL_TIMEOUT=600` combined with a `grub-common.service` shutdown drop-in holds GRUB in RAM for 10 minutes after an unclean power loss (keeping encrypted disks unmounted during unstable power), while normal `reboot`s boot in `0s`. TPM2 dictionary-attack lockout counters are cleared automatically on successful boot.
2. **"Bunker Mode" Network Isolation (`scripts/bunker_enderhub.sh`)**
   - **Zero public exposure**: services bind strictly to the **Tailscale VPN IP**, trusted LAN IPv4 subnet, or `127.0.0.1` (no `0.0.0.0` or public IPv6 `[::]`).
   - **Tailscale Exit Node & Subnet Router** with `rx-udp-gro-forwarding` optimization (`scripts/enable_tailscale_router_and_pairdrop.sh`).
   - **UFW + Fail2ban** (monitoring SSH even on LAN) + kernel `sysctl` hardening (`tcp_syncookies`, disabled ICMP redirects/source routing, `kptr_restrict=2`, `dmesg_restrict=1`).
3. **Zero-Dependency Telegram Control Plane (`docker-bot/`)**
   - Native Python 3.12 stdlib daemon (`bot.py` + `stacks.py`) using outbound HTTPS long-polling (zero open router ports) and an exclusive `flock` single-instance lock (`ensure_bot.sh`).
   - Controls 12+ Docker Compose stacks by category, checks port conflicts, reports CPU/GPU/SMART metrics, detects hardware reboots via `/proc/sys/kernel/random/boot_id`, and respects intentional manual stops.
4. **Self-Healing Watchdogs & Hot Backups (`scripts/`)**
   - `self_heal_ports.sh`: cron watchdog verifying Tailscale socket bindings after boot and restarting stopped-by-race stacks (ignoring stacks manually stopped via Telegram).
   - `backup_configs.sh`: nightly hot `mariadb-dump`, Open-WebUI SQLite DB backup, Docker Compose files, and system configs with 7-snapshot rotation.
5. **Observability & Local AI (`monitoring/`, `cli/`)**
   - Provisioned Grafana dashboard (`monitoring/enderhub-command-center.json`) tracking NVIDIA GPU temps/VRAM/power (`dcgm-exporter`), CPU/RAM/disk (`node-exporter`), and LAN/Tailscale traffic.
   - `cli/ender-ai`: lightweight streaming CLI client for Ollama with 30-minute terminal session memory.

## 📁 Repository Layout

```text
enderhub/
├── cli/
│   └── ender-ai                                 # Streaming CLI client for Ollama
├── docker-bot/
│   ├── .env.example                             # Required environment variables
│   ├── bot.py                                   # Zero-dependency Telegram Docker controller
│   ├── ensure_bot.sh                            # flock + log-rotation watchdog
│   └── stacks.py                                # Parameterized Docker Compose stack registry
├── examples/
│   └── vscode_continue_config.example.json      # Continue.dev Ollama remote config template
├── monitoring/
│   └── enderhub-command-center.json             # Grafana dashboard JSON
└── scripts/
    ├── backup_configs.sh                        # Nightly hot DB & config backup
    ├── backup_minecraft_script.sh               # Diff-aware game server backup
    ├── bunker_enderhub.sh                       # UFW, Fail2ban, sysctl & Docker IP binding
    ├── enable_tailscale_router_and_pairdrop.sh  # Exit Node + Subnet Router + GRO tuning
    ├── ensure_nc_ai_worker.sh                   # Self-heal + Nextcloud AI worker watchdog
    ├── self_heal_ports.sh                       # Tailscale bind self-healing watchdog
    └── upgrade_nextcloud_safe.sh                # Sequential major-version Nextcloud upgrader
```

---

# 🇮🇹 Italiano

# EnderHub — Home Server Linux Resiliente, Modalità Bunker & Stack AI

Architettura di riferimento e suite di automazione per un server domestico bare-metal **Ubuntu 24.04 LTS** h24 basato su **LUKS2 + TPM 2.0**, **LVM2**, rete mesh **Tailscale WireGuard**, **Docker Compose**, osservabilità **Prometheus/Grafana**, **inferenza LLM locale su GPU (Ollama + Open-WebUI)** e **bot Telegram di controllo a zero dipendenze esterne**.

> [!NOTE]
> Tutti gli indirizzi IP, seriali dei dischi, UUID, ID utente e credenziali sono stati rimossi o parametrizzati tramite variabili d'ambiente.

## 🏗️ Punti Chiave dell'Architettura

1. **Storage Cifrato e Resiliente (LUKS2 + TPM 2.0 + LVM2)**
   - Partizione di sistema `/` protetta da cifratura **LUKS2** con sblocco automatico tramite **TPM 2.0 (`clevis`)**.
   - Pool **LVM2** multi-SSD (`/mnt`) dedicato a Nextcloud, MariaDB e Redis.
   - **Scudo Anti-Blackout**: `GRUB_RECORDFAIL_TIMEOUT=600` combinato con un drop-in systemd di `grub-common.service` mantiene il server fermo in RAM per 10 minuti dopo un blackout improvviso (lasciando i dischi smontati durante eventuali sbalzi), mentre i riavvii puliti partono in `0s`.
2. **Isolamento "Modalità Bunker" (`scripts/bunker_enderhub.sh`)**
   - **Zero esposizione pubblica**: bind esclusivo su IP **Tailscale**, sottorete LAN fidata o `127.0.0.1` (nessun bind su `0.0.0.0` o IPv6 `[::]`).
   - **Tailscale Exit Node & Subnet Router** con ottimizzazione `rx-udp-gro-forwarding`.
   - **UFW + Fail2ban** (attivo anche su LAN) + hardening kernel `sysctl`.
3. **Bot Telegram in Pure Python Stdlib (`docker-bot/`)**
   - Demone Python 3.12 senza librerie `pip` esterne (`bot.py` + `stacks.py`) con long-polling HTTPS in uscita (zero porte aperte sul router) e lock esclusivo `flock` (`ensure_bot.sh`).
   - Gestisce oltre 12 stack Compose, controlla conflitti di porta, monitora temperature/VRAM/SMART e rileva riavvii hardware reali tramite `boot_id`.
4. **Watchdog & Backup Notturni (`scripts/`)**
   - `self_heal_ports.sh`: ripristina eventuali bind Tailscale persi al boot rispettando gli stop manuali dal bot.
   - `backup_configs.sh`: dump a caldo MariaDB, DB Open-WebUI e configurazioni di sistema con rotazione degli ultimi 7 snapshot.
5. **Osservabilità & AI Locale (`monitoring/`, `cli/`)**
   - Dashboard Grafana (`monitoring/enderhub-command-center.json`) con metriche GPU NVIDIA (`dcgm-exporter`), host (`node-exporter`) e rete.
   - `cli/ender-ai`: client CLI in streaming verso Ollama con memoria di sessione da 30 minuti.
