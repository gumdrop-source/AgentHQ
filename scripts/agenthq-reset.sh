#!/bin/bash
# AgentHQ Reset — wipe an existing install and re-run the bootstrap.
#
# Use this on a dev/test box to validate the install end-to-end against
# a clean slate (without reinstalling Ubuntu). Removes:
#   - all agents (and their per-agent credentials)
#   - the agent-control-web service + agent@ template unit
#   - /opt/AgentHQ, /opt/agents, /opt/agent-control-web
#   - /etc/agents, /var/lib/agent-control
#   - /usr/local/bin/agent-control, /usr/local/bin/agenthq-cred
#   - the 'agents' group
# Leaves: apt packages (bun, uv, cloudflared, python, etc.), the bootstrap
# admin user (you), and Ubuntu's own state.
#
# Then runs the official curl-bash installer to rebuild from main.

set -e

if [[ $EUID -ne 0 ]]; then
    exec sudo -E bash "$0" "$@"
fi

echo "═══════════════════════════════════════════════════════════════"
echo " AgentHQ Reset — wiping and reinstalling"
echo "═══════════════════════════════════════════════════════════════"
echo

if [[ "${AGENTHQ_RESET_YES:-}" != "1" ]]; then
    read -p "Wipe AgentHQ and reinstall from main? [y/N] " yn
    [[ "$yn" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
fi

echo
echo "── Tearing down existing agents ──"
shopt -s nullglob
for dropin in /etc/systemd/system/agent@*.service.d; do
    name="$(basename "$dropin" | sed 's/agent@//; s/.service.d//')"
    if command -v agent-control >/dev/null; then
        agent-control delete "$name" --purge 2>/dev/null || true
    fi
done

echo "── Stopping and removing services ──"
systemctl disable --now agent-control-web.service 2>/dev/null || true
rm -f /etc/systemd/system/agent-control-web.service \
      /etc/systemd/system/agent@.service \
      /etc/systemd/system/multi-user.target.wants/agent-control-web.service
rm -rf /etc/systemd/system/agent@*.service.d
systemctl daemon-reload

echo "── Wiping platform files ──"
rm -rf /opt/AgentHQ /opt/agents /opt/agent-control-web
rm -rf /etc/agents /var/lib/agent-control
rm -f /usr/local/bin/agent-control /usr/local/bin/agenthq-cred

echo "── Removing 'agents' group ──"
groupdel agents 2>/dev/null || true

echo "── Sweeping leftover agent home directories ──"
for d in /home/*/; do
    [[ -f "$d/agent.toml" ]] || continue
    user="$(basename "$d")"
    echo "    removing /home/$user"
    rm -rf "$d"
    userdel "$user" 2>/dev/null || true
done
shopt -u nullglob

echo
echo "═══════════════════════════════════════════════════════════════"
echo " Reset complete. Re-running install.sh from main..."
echo "═══════════════════════════════════════════════════════════════"
echo

curl -fsSL https://raw.githubusercontent.com/gumdrop-source/AgentHQ/main/install.sh | bash

echo
echo "Done. Open http://localhost:5000 to start the wizard."
read -p "Press Enter to close." _
