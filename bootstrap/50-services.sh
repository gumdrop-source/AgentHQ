#!/usr/bin/env bash
# Phase 50 — systemd unit templates for the platform + per-agent instances
#
# Plan: use systemd templated units (agent@.service) so a single template
# spawns one instance per agent. agent-control just calls
#   systemctl enable --now agent@alice.service
# and a fresh instance comes up — no copy-paste of unit files per agent.
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"
require_root

src="$(agenthq_root)/templates/systemd"

if [[ ! -d "$src" ]]; then
    die "templates/systemd/ missing — repo is broken"
fi

log "Installing agent-prelaunch → /opt/agents/bin"
install -m 0755 -o root -g agents \
    "$(agenthq_root)/bin/agent-prelaunch" /opt/agents/bin/agent-prelaunch

log "Installing systemd unit templates → /etc/systemd/system/"
rsync -a "$src/" /etc/systemd/system/
systemctl daemon-reload

# Per-agent templated units (agent@.service) are NOT enabled here.
# agent-control enables agent@<name>.service when it provisions an agent.

log "Phase 50 complete"
