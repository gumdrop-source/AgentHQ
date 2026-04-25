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
    log "No systemd templates yet (templates/systemd/ missing) — skipping"
    log "Phase 50 complete (no-op)"
    exit 0
fi

log "Installing systemd unit templates → /etc/systemd/system/"
rsync -a "$src/" /etc/systemd/system/
systemctl daemon-reload

# TODO: enable the platform-level services (agent-control), but NOT per-agent
# templated units (those get enabled by agent-control on demand).

log "Phase 50 complete"
