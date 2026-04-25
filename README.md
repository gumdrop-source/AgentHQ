# AgentHQ

Multi-tenant Claude Code agent platform for Ubuntu.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/gumdrop-source/AgentHQ/main/install.sh | bash
```

Then provision your first agent:

```sh
sudo agent-control create alice --tools m365,gmail,myob,telegram
```

## Architecture

```
/opt/agents/
  tools/          shared MCP tool library
  skills/         shared skill playbooks
  bin/            shared binaries (claude)
  templates/      per-agent config templates

/opt/agent-control/         control plane app
/etc/agents/credentials/    systemd-creds vault (TPM2 if available, host-key fallback)
/var/lib/agent-control/     control plane sqlite

/home/<agent>/              one directory per agent
  agent.toml                tool list, persona, telegram chat id
  skills/                   agent-private skill overrides
  memory/                   facts, entities, conv_log
  .claude/                  claude session + plugins
  .claude-mem/              per-agent memory db
```

The principle: an agent is a *configuration*, not a codebase. Tool code
lives once in `/opt/agents/tools/` and is composed per agent via
`agent.toml`. Adding a new agent is a config change, not a copy-paste.

## Status

🚧 Pre-alpha. The bootstrap repo is being scaffolded — phase scripts are
skeletons with TODO blocks, no agent-control implementation yet.

See `docs/architecture.md` for the design rationale.
