#!/usr/bin/env bash
# Phase 40 — claude config templates + plugin cache
#
# Decision: claude binary is installed PER AGENT, not system-wide.
# Claude is a ~240 MB self-updating ELF that lives at
# $HOME/.local/share/claude/versions/<X>/. Per-agent install matches the
# official installer, lets self-update work without root, and isolates
# one agent's broken claude from the rest.
#
# So this phase is light. agent-control does the heavy lifting at
# provision time:
#   1. Run the official installer as the new agent
#   2. Render templates from /opt/agents/templates/ into the agent's HOME
#   3. Pre-warm plugin cache (telegram, claude-mem)
#
# This phase encodes the five onboarding gates that silently blocked the
# 2026-04-25 Alice cutover by staging the templates that carry them:
#   1. hasCompletedOnboarding
#   2. theme + lastOnboardingVersion
#   3. projects.<home>.hasTrustDialogAccepted
#   4. enabledMcpjsonServers + enableAllProjectMcpServers
#   5. enabledPlugins
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"
require_root

# Stage templates → /opt/agents/templates/ (read by agent-control at create time)
src_tmpl="$(agenthq_root)/templates"
dst_tmpl=/opt/agents/templates
log "Staging templates → $dst_tmpl"
rsync -a --exclude='systemd' "$src_tmpl/" "$dst_tmpl/"
chown -R root:agents "$dst_tmpl"
chmod -R g+rX "$dst_tmpl"

# Verify the claude installer is reachable so we fail fast at bootstrap
# rather than at first agent provisioning
log "Pinging claude installer endpoint"
if ! curl -fsSL --max-time 10 -o /dev/null -I https://claude.ai/install.sh; then
    log "WARNING: claude installer unreachable — agent-control will need network at provision time"
fi

# TODO: optionally pre-cache the claude binary at /opt/agents/cache/claude-<version>
# so first-agent provisioning doesn't have to download 240 MB. Skipping for now —
# agent-control can curl it lazily on first create.

# TODO: pre-warm plugin tarballs (telegram, claude-mem) into /opt/agents/cache/plugins/
# for the same reason.

log "Phase 40 complete (templates staged; claude binary install deferred to agent-control)"
