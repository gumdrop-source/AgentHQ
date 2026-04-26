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

log "Installing agent-control → /usr/local/bin"
install -m 0755 \
    "$(agenthq_root)/agent-control/agent-control" /usr/local/bin/agent-control

# Web setup wizard
log "Installing agent-control-web → /opt/agent-control-web"
rm -rf /opt/agent-control-web
mkdir -p /opt/agent-control-web
rsync -a "$(agenthq_root)/agent-control-web/" /opt/agent-control-web/
( cd /opt/agent-control-web && /usr/local/bin/bun install --silent )

log "Installing systemd unit templates → /etc/systemd/system/"
rsync -a "$src/" /etc/systemd/system/
systemctl daemon-reload

# Enable + start the web wizard so install.sh's final line can point to it.
# Per-agent templated units (agent@.service) are NOT enabled here — those
# come up via agent-control / the web wizard at provisioning time.
systemctl enable --now agent-control-web.service

log "Phase 50 complete"
