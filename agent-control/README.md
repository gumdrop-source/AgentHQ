# agent-control

The control-plane CLI. Provisions, configures, and tears down agents
on this AgentHQ host.

Installed by Phase 50 of the bootstrap → `/usr/local/bin/agent-control`.

## Commands

```sh
sudo agent-control create <name> --tools t1,t2,... [--persona "..."] [--telegram-chat-id N]
sudo agent-control delete <name> [--purge]
sudo agent-control list
```

## What `create` does

1. **Linux user** — `useradd <name>` in the `agents` group, home `/home/<name>`
2. **Home tree** — creates `memory/`, `skills/`, `logs/`, `.claude/`
3. **Templates** — renders `agent.toml`, `.claude.json`, `settings.json`
   from `/opt/agents/templates/`. The `.claude.json` carries the four
   onboarding gates so the agent's first claude invocation is non-interactive
4. **Claude binary** — runs the official installer as the agent user
   (per-agent install at `~/.local/share/claude/versions/<X>/`)
5. **Credential drop-in** — writes
   `/etc/systemd/system/agent@<name>.service.d/credentials.conf` with
   `LoadCredentialEncrypted=` lines for the agent's enabled tools
6. **Service** — `systemctl enable --now agent@<name>.service`

The tool-to-credentials mapping currently lives inside `agent-control`'s
`write_credentials_dropin` function. As real tools land under `/opt/agents/tools/`,
that mapping should move into per-tool `tool.toml` files (see `tools/README.md`).

## What `delete` does

- `systemctl disable --now agent@<name>.service`
- Removes the credential drop-in directory
- `userdel <name>` (preserves home) or `userdel --remove <name>` with `--purge`

## What's not here yet

- `update` — change tool list / persona without delete-recreate
- `restart` — convenience wrapper over `systemctl restart`
- `logs <name>` — wrapper over `journalctl -u agent@<name>`
- A real config DB at `/var/lib/agent-control/control.db` for richer state
- Web UI

## Prior art

A Python prototype lives at `/home/alice/agent-control/` (removed's
existing system). This bash MVP captures the essential creation flow
without the schema/migration overhead. We can replace it with a richer
implementation later when the requirements are clearer.
