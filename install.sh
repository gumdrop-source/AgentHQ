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

# Elevate to root if not already
if [[ $EUID -ne 0 ]]; then
    echo "[AgentHQ] Re-running under sudo..."
    exec sudo -E env \
        AGENTHQ_REPO_URL="$REPO_URL" \
        AGENTHQ_REPO_REF="$REPO_REF" \
        AGENTHQ_INSTALL_DIR="$INSTALL_DIR" \
        AGENTHQ_LOCAL_SUBNET="${AGENTHQ_LOCAL_SUBNET:-}" \
        bash -c "curl -fsSL $REPO_URL/raw/$REPO_REF/install.sh | bash"
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

# Fetch the repo
echo "[AgentHQ] Cloning $REPO_URL ($REPO_REF) → $INSTALL_DIR"
rm -rf "$INSTALL_DIR"
git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$INSTALL_DIR"

# Run phases in order
cd "$INSTALL_DIR"
# shellcheck source=bootstrap/lib.sh
. bootstrap/lib.sh

for phase in bootstrap/[0-9][0-9]-*.sh; do
    log_section "$(basename "$phase" .sh)"
    bash "$phase"
done

echo
echo "[AgentHQ] Platform install complete."
echo "Next:  sudo agent-control create <agent-name> --tools=tool1,tool2,..."
