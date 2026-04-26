#!/usr/bin/env bash
# AgentHQ installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/gumdrop-source/AgentHQ/main/install.sh | bash
#
# Bootstraps a fresh Ubuntu host into a multi-tenant Claude Code agent platform.
# After install:
#   sudo agent-control create <name> --tools m365,gmail,...
#
# Environment overrides (rarely needed):
#   AGENTHQ_REPO_URL      git remote (default: https://github.com/gumdrop-source/AgentHQ.git)
#   AGENTHQ_REPO_REF      branch / tag (default: main)
#   AGENTHQ_INSTALL_DIR   clone target (default: /opt/AgentHQ)
#   AGENTHQ_LOCAL_SUBNET  CIDR allowed inbound on RDP (default: 192.168.0.0/16)

set -euo pipefail

REPO_URL="${AGENTHQ_REPO_URL:-https://github.com/gumdrop-source/AgentHQ.git}"
REPO_REF="${AGENTHQ_REPO_REF:-main}"
INSTALL_DIR="${AGENTHQ_INSTALL_DIR:-/opt/AgentHQ}"

# Raw URL — derived from REPO_URL, used to re-fetch this script under sudo.
# raw.githubusercontent.com is the right host (https://github.com/.../raw/...
# would also redirect, but only without the .git suffix).
RAW_URL="${AGENTHQ_RAW_URL:-https://raw.githubusercontent.com/gumdrop-source/AgentHQ}"

# Elevate to root if not already
if [[ $EUID -ne 0 ]]; then
    echo "[AgentHQ] Re-running under sudo..."
    exec sudo -E env \
        AGENTHQ_REPO_URL="$REPO_URL" \
        AGENTHQ_RAW_URL="$RAW_URL" \
        AGENTHQ_REPO_REF="$REPO_REF" \
        AGENTHQ_INSTALL_DIR="$INSTALL_DIR" \
        AGENTHQ_LOCAL_SUBNET="${AGENTHQ_LOCAL_SUBNET:-}" \
        bash -c "curl -fsSL $RAW_URL/$REPO_REF/install.sh | bash"
fi

# OS gate
. /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]]; then
    echo "[AgentHQ][ERROR] Requires Ubuntu. Detected: ${PRETTY_NAME:-unknown}" >&2
    exit 1
fi
if (( ${VERSION_ID%%.*} < 24 )); then
    echo "[AgentHQ][ERROR] Requires Ubuntu 24.04 or newer. Detected: $PRETTY_NAME" >&2
    exit 1
fi

# Minimum deps to clone the repo
apt-get update -qq
apt-get install -y -qq git ca-certificates curl

# Fetch the repo (only on first invocation — see self-update guard below)
if [[ "${AGENTHQ_BOOTSTRAPPED:-}" != "1" ]]; then
    echo "[AgentHQ] Cloning $REPO_URL ($REPO_REF) → $INSTALL_DIR"
    rm -rf "$INSTALL_DIR"
    git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$INSTALL_DIR"

    # Self-update: re-exec the freshly-cloned install.sh so subsequent phase
    # logic AND the final summary message all come from the latest version.
    # Without this, repeated runs from /opt/AgentHQ/install.sh keep using
    # the old in-memory script for the trailing lines (the inode is still
    # held even after rm -rf).
    exec env AGENTHQ_BOOTSTRAPPED=1 \
        AGENTHQ_REPO_URL="$REPO_URL" \
        AGENTHQ_RAW_URL="$RAW_URL" \
        AGENTHQ_REPO_REF="$REPO_REF" \
        AGENTHQ_INSTALL_DIR="$INSTALL_DIR" \
        AGENTHQ_LOCAL_SUBNET="${AGENTHQ_LOCAL_SUBNET:-}" \
        bash "$INSTALL_DIR/install.sh"
fi

# Run phases in order
cd "$INSTALL_DIR"
# shellcheck source=bootstrap/lib.sh
. bootstrap/lib.sh

for phase in bootstrap/[0-9][0-9]-*.sh; do
    log_section "$(basename "$phase" .sh)"
    bash "$phase"
done

echo
echo "════════════════════════════════════════════════════════════════"
echo " AgentHQ platform install complete."
echo
echo " Open the setup wizard in a browser on this box:"
echo "   http://localhost:5000"
echo
echo " Or from another machine on the LAN, SSH-tunnel:"
echo "   ssh -L 5000:localhost:5000 $(whoami)@$(hostname -I | awk '{print $1}')"
echo "   then open  http://localhost:5000"
echo
echo " Power users can skip the wizard and use the CLI:"
echo "   sudo agent-control create <name> --tools telegram --telegram-chat-id <N>"
echo "════════════════════════════════════════════════════════════════"
