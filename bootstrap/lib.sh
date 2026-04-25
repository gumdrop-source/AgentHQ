# AgentHQ bootstrap helpers — sourced by every phase script
# shellcheck shell=bash

set -euo pipefail

log() { echo "[AgentHQ] $*"; }

log_section() {
    local title="$1"
    local pad
    pad="$(printf '═%.0s' $(seq 1 $((60 - ${#title}))))"
    printf '\n═══ %s %s\n' "$title" "$pad"
}

die() {
    echo "[AgentHQ][ERROR] $*" >&2
    exit 1
}

require_root() {
    [[ $EUID -eq 0 ]] || die "must run as root (invoke via install.sh, not directly)"
}

# Idempotent apt install — only fetches packages that aren't already installed
apt_ensure() {
    local missing=()
    local pkg
    for pkg in "$@"; do
        if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
            missing+=("$pkg")
        fi
    done
    if (( ${#missing[@]} > 0 )); then
        log "apt install: ${missing[*]}"
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
    fi
}

# Resolve the AgentHQ install root regardless of which phase script is calling
agenthq_root() {
    cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd
}
