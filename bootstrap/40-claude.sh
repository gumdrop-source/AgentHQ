#!/usr/bin/env bash
# Phase 40 — claude binary + plugin cache + onboarding-gate templates
#
# Encodes the 5 onboarding gates that silently blocked claude after the
# 2026-04-25 Alice cutover. agent-control renders these templates per
# agent at provision time so the very first claude invocation works.
#
# Gates:
#   1. hasCompletedOnboarding     — skips welcome screen
#   2. theme + lastOnboardingVersion — skips theme picker
#   3. projects.<home>.hasTrustDialogAccepted — skips workspace trust prompt
#   4. enabledMcpjsonServers + enableAllProjectMcpServers — auto-approve .mcp.json servers
#   5. enabledPlugins             — without this, plugins listed but reported "0 enabled"
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"
require_root

# Stage the templates — agent-control renders them per agent
src_tmpl="$(agenthq_root)/templates"
dst_tmpl=/opt/agents/templates
log "Staging templates → $dst_tmpl"
rsync -a "$src_tmpl/" "$dst_tmpl/"
chown -R root:agents "$dst_tmpl"
chmod -R g+rX "$dst_tmpl"

# TODO: install claude binary to /opt/agents/bin/claude
#   Decide between:
#   (a) per-agent npm install (current Alice has it at ~/.local/bin/claude)
#   (b) shared binary at /opt/agents/bin/claude with per-agent config in $HOME
#   (b) is cleaner — one upgrade, all agents — but needs --config-dir support per invocation.

# TODO: prefetch plugin caches so first invocation doesn't block on download:
#   - claude-plugins-official/telegram (pinned version)
#   - thedotmack/claude-mem (pinned version)
# Plugins live in $HOME/.claude/plugins/cache/ today; for shared install we'd
# either symlink or pre-warm at /opt/agents/plugins/.

log "Phase 40 placeholder — claude binary install + plugin prefetch are TODO"
