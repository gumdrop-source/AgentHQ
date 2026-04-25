#!/usr/bin/env bash
# Phase 10 — platform group + directory tree
# Per-agent home dirs are NOT created here; agent-control creates them
# on demand via `agent-control create <name>`.
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"
require_root

log "Creating 'agents' group"
getent group agents >/dev/null || groupadd --system agents

log "Creating platform directories"
install -d -o root -g agents -m 0755 /opt/agents
install -d -o root -g agents -m 0755 /opt/agents/tools
install -d -o root -g agents -m 0755 /opt/agents/skills
install -d -o root -g agents -m 0755 /opt/agents/bin
install -d -o root -g agents -m 0755 /opt/agents/templates
install -d -o root -g root   -m 0755 /opt/agent-control

install -d -o root -g agents -m 0750 /etc/agents
install -d -o root -g root   -m 0700 /etc/agents/credentials
install -d -o root -g root   -m 0755 /var/lib/agent-control

log "Phase 10 complete"
