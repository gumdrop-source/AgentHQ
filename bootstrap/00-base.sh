#!/usr/bin/env bash
# Phase 00 — base packages, runtimes, firewall, unattended upgrades
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"
require_root

apt-get update -qq

apt_ensure \
    python3 python3-venv python3-pip python3-dev \
    build-essential git curl wget ca-certificates rsync \
    sqlite3 jq \
    ufw unattended-upgrades \
    tpm2-tools

# bun — installed system-wide so every agent's claude session can spawn the
# telegram plugin's bun-based bot poller (this was a silent blocker on
# the 2026-04-25 Alice cutover; documenting here so it stays fixed).
if ! command -v bun >/dev/null; then
    log "Installing bun → /usr/local"
    curl -fsSL https://bun.sh/install | BUN_INSTALL=/usr/local bash >/dev/null
fi

# uv — used by claude-mem's python tooling
if ! command -v uv >/dev/null; then
    log "Installing uv → /usr/local/bin"
    curl -fsSL https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh >/dev/null
fi

# cloudflared — for Vapi/voice tunnels (optional per agent, but install once at platform level)
if ! command -v cloudflared >/dev/null; then
    log "Installing cloudflared → /usr/local/bin"
    curl -fsSL -o /usr/local/bin/cloudflared \
        https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
    chmod +x /usr/local/bin/cloudflared
fi

# Firewall — deny incoming, allow SSH from anywhere, RDP from local subnet only
log "Configuring ufw"
ufw --force default deny incoming  >/dev/null
ufw --force default allow outgoing >/dev/null
ufw allow 22/tcp comment 'ssh' >/dev/null
local_subnet="${AGENTHQ_LOCAL_SUBNET:-192.168.0.0/16}"
ufw allow from "$local_subnet" to any port 3389 comment 'rdp local' >/dev/null
ufw --force enable >/dev/null

log "Enabling unattended-upgrades"
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null 2>&1 || true

log "Phase 00 complete"
