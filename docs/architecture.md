# AgentHQ — Architecture

## Soul and purpose

1. **An agent is a configuration, not a codebase.** Standing up a new
   agent should be a config change, not a copy-paste. Tool code lives
   once on the host; agents compose tools via their `agent.toml`.

2. **The setup script is the spec.** Not docs, not memory notes, not
   tribal knowledge. If the box dies, `install.sh` rebuilds it. No
   manual steps that someone has to remember.

3. **Tools live once, agents compose them.** A vendor wrapper (MYOB,
   M365, Gmail, …) belongs to the host, not to any single agent.
   Multiple agents share the same tool implementation; credentials and
   permissions are gated per agent.

4. **Data is sacred, infrastructure is cattle.** Agent memory
   (claude-mem chroma, conv_log, facts.json), control-plane state,
   accumulated knowledge — all carried forward across reinstalls.
   Everything else is reproducible from the repo.

5. **Multi-tenant from day one.** A single host runs many agents.
   Each has its own home, its own credentials, its own Telegram bot,
   its own memory store. They share only the tool library and the
   control plane.

## Filesystem layout

```
/opt/agents/
├── tools/        ← MCP servers, owned root:agents, agents have read-only access
├── skills/       ← shared skill playbooks
├── bin/          ← shared binaries (claude)
└── templates/    ← config templates rendered per-agent

/opt/agent-control/        ← control plane app
/etc/agents/credentials/   ← systemd-creds vault (TPM2 or host-key)
/var/lib/agent-control/    ← control plane sqlite

/home/<agent>/             ← one directory per agent
├── agent.toml             ← tool list + persona + telegram binding
├── skills/                ← agent-private skill overrides
├── memory/                ← facts.json, entities.json, conv_log.md
├── .claude/               ← session + plugins (per-agent)
└── .claude-mem/chroma/    ← per-agent vector memory
```

## Why these specific paths

- `/opt/agents/` is **agent-neutral**. Calling it `/opt/alice/` would
  imply a single agent owns the platform; here Alice is one tenant
  among many.
- Per-agent state in `$HOME/<agent>/` keeps standard Linux conventions
  and lets per-user systemd / RDP / claude all work without surprise.
- Credentials in `/etc/agents/credentials/` (root:root 0700) mean only
  systemd services can decrypt them via `LoadCredentialEncrypted=`.
  Agents never see raw secrets in their environment.

## Onboarding gates (the 5 things that broke Alice on 2026-04-25)

A fresh `claude` invocation silently waits on five interactive gates.
The bootstrap script encodes each as code so the first invocation
just works:

1. **Welcome screen** — `hasCompletedOnboarding: true` in `.claude.json`
2. **Theme picker** — `theme` and `lastOnboardingVersion` in `.claude.json`
3. **Workspace trust** — `projects.<home>.hasTrustDialogAccepted: true`
4. **MCP server approval** — `enabledMcpjsonServers` per project +
   `enableAllProjectMcpServers: true` in settings.json
5. **Plugin enablement** — `enabledPlugins` in settings.json. Without
   this, `installed_plugins.json` lists plugins but the runtime
   reports "0 enabled" and never spawns plugin MCP servers.

Templates in `templates/` carry these gates. agent-control renders
them per-agent at provision time.

## Bootstrap phases

| Phase | Script | Purpose |
|-------|--------|---------|
| 00 | `00-base.sh` | apt deps, bun, uv, cloudflared, ufw, unattended-upgrades |
| 10 | `10-users.sh` | `agents` group, /opt/agents tree, /etc/agents tree |
| 20 | `20-credentials.sh` | systemd-creds vault (TPM2 or host-key) |
| 30 | `30-tools.sh` | sync tool library to /opt/agents/tools/, build venvs |
| 40 | `40-claude.sh` | claude binary, plugin cache, onboarding-gate templates |
| 50 | `50-services.sh` | systemd unit templates (`agent@.service`, etc.) |

Per-agent provisioning happens later via `agent-control create`,
not as part of bootstrap.

## Claude binary — per-agent install

Decision: install claude **per agent**, not system-wide.

Claude is a ~240 MB self-updating ELF that lives at
`$HOME/.local/share/claude/versions/<X>/` with a symlink at
`$HOME/.local/bin/claude`. Each agent runs the official installer at
provision time (`agent-control create` does this), giving each agent
its own copy.

Why per-agent over shared `/opt/agents/bin/claude`:
- Matches the official installer's design (no special prefix flags needed)
- Claude self-update writes into `$HOME` — works without root
- One agent's broken or mid-update claude can't take down others
- Disk cost (~240 MB × N agents) is acceptable for a fleet of 2–10

Phase 40 stages config templates only. The actual binary install is
deferred to agent-control.

## Open questions

- **TPM2 firmware gate.** Agents-01 currently runs Infineon SLB9670
  FW 7.62 (CVE-2025-2884). Bootstrap should detect FW version and
  refuse TPM2 mode below 7.86, falling back to host-key.
- **agent-control** — port the existing prototype at
  `/home/alice/agent-control/` or rewrite cleanly inside AgentHQ?
