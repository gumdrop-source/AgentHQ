#!/usr/bin/env bash
# Phase 30 — install MCP tool library to /opt/agents/tools/
#
# Each subdirectory of /opt/AgentHQ/tools/ is a self-contained MCP server
# with its own requirements.txt (Python) or package.json (Node).
# shellcheck source=lib.sh
. "$(dirname "$0")/lib.sh"
require_root

src="$(agenthq_root)/tools"
dst=/opt/agents/tools

if [[ ! -d "$src" ]] || [[ -z "$(ls -A "$src" 2>/dev/null | grep -v '^README')" ]]; then
    log "No tools to install yet (tools/ is empty) — skipping"
    log "Phase 30 complete (no-op)"
    exit 0
fi

log "Syncing tools $src → $dst"
for tool_dir in "$src"/*/; do
    [[ -d "$tool_dir" ]] || continue
    name="$(basename "$tool_dir")"
    log "  $name"
    rsync -a --delete "$tool_dir" "$dst/$name/"

    if [[ -f "$dst/$name/requirements.txt" ]]; then
        python3 -m venv "$dst/$name/.venv"
        "$dst/$name/.venv/bin/pip" install --quiet -r "$dst/$name/requirements.txt"
    fi

    if [[ -f "$dst/$name/package.json" ]]; then
        ( cd "$dst/$name" && bun install --silent )
    fi
done

chown -R root:agents "$dst"
chmod -R g+rX,o-rwx "$dst"

log "Phase 30 complete"
