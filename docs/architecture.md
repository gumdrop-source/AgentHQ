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

## Integration model — interactive setup wizards

Tools ship with the platform but start **inactive**. The user activates
integrations one at a time through `agent-control integrations`, which
walks them through registering each external service, prompts for the
credentials, validates them with a test API call, and stores them in
the vault.

```
sudo agent-control integrations               # list catalog + active state
sudo agent-control integrations enable m365   # run wizard for M365
sudo agent-control integrations disable m365  # remove creds, mark inactive
```

This means:

- A fresh AgentHQ install has zero credentials. Nothing is "wired up"
  until the user activates it. That's the public-grade discipline —
  anyone can clone the repo without inheriting anything sensitive.
- Adding an integration is one guided flow per integration, not a
  sequence of "where do I find this token" lookups across docs.
- `agent-control create alice` auto-includes every *active*
  integration — no `--tools` flag, no manual cred mapping. New
  agents inherit the host's wired-up integrations by default.
- Per-agent restriction (e.g. Allen for sales gets no MYOB) is opt-out
  via `disabled_tools` in `agent.toml`.

### Per-tool repo layout

```
tools/<name>/
├── setup.md           human setup instructions (copy-paste-friendly)
├── setup.json         cred schema — fields, descriptions, validation rules
├── tool.toml          metadata: name, version, description
├── server.py          MCP server (Python)  — or server.ts for Node
└── requirements.txt   (or package.json)
```

`setup.json` example:

```json
{
  "name": "m365",
  "credentials": [
    {
      "key": "tenant_id",
      "prompt": "Microsoft 365 tenant ID",
      "description": "Found in Azure Portal → Microsoft Entra ID → Overview",
      "validate": "uuid"
    },
    {
      "key": "client_id",
      "prompt": "Application (client) ID",
      "validate": "uuid"
    },
    {
      "key": "client_secret",
      "prompt": "Client secret value",
      "secret": true
    }
  ],
  "test": "python -m m365 --self-test"
}
```

The current `agent-control` bash MVP doesn't implement this yet — it
hardcodes a tool→cred mapping. The refactor to read `setup.json` and
run the wizards is queued behind the basic install smoke test.

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

## Connectors: AgentHQ owns its integrations

AgentHQ deliberately does NOT use Anthropic-hosted account-level
connectors (e.g. `mcp__claude_ai_Microsoft_365__*`,
`mcp__claude_ai_Gmail__*`). They look convenient — tick a checkbox at
claude.ai and every claude session signed into that account inherits
the connector — but they bypass the per-agent isolation that's central
to AgentHQ's design.

**The problem with account-level connectors**

A single Anthropic account login on multiple agents means every agent
inherits every connector that account has enabled. If an admin
authenticates Daisy, Allen, and Bob with one Anthropic account and
that account has the M365 connector enabled, all three agents have
access to the same M365 mailbox — *whoever's mailbox the account is
linked to*. The vault, the per-agent permission matrix, the
agent-prefixed credentials — none of it gates Anthropic-hosted
connectors. They're managed entirely by Anthropic.

This was discovered during testing: a fresh `testbot` agent with no
local M365 setup was nonetheless able to read the operator's real
inbox, because testbot's claude was signed into the operator's
Anthropic account and that account had M365 connected at claude.ai.

**The decision**

For any service where AgentHQ provides per-agent isolation
(per-tenant, per-user, per-mailbox), build the connector inside
AgentHQ:

- Tool manifest at `tools/<name>/tool.json`
- MCP server at `tools/<name>/server.py` (or `server.ts`)
- Credentials in `/etc/agents/credentials/<key>.cred`
  (vault, per-agent prefixed where appropriate)
- Per-agent OAuth via the in-conversation device flow
  (`tools/<name>/auth.py` driven by `agent-control-web`'s wizard)
- Granted per agent via the permission matrix

Anthropic-hosted connectors are explicitly avoided. To enforce this
on each AgentHQ host:

- Operator disconnects all M365/Gmail/etc connectors at
  <https://claude.ai/settings/connectors>
- Each agent's `settings.json` lists the AgentHQ-managed MCP servers
  it's allowed to call; nothing else (no `mcp__claude_ai_*` entries)
- Future: bake a `disableAccountConnectors: true` (or equivalent
  claude-code setting) into the agent template

**When account-level connectors are fine**

- A single-user setup where the operator is the only person, and
  doesn't care that any spawned agent inherits the same connectors.
- Personal-use Alice running on the operator's own mailbox.

For multi-tenant — Daisy in accounts, Allen in sales, etc. — the
account-level model breaks down and AgentHQ-owned connectors are the
only correct path.

## Credential isolation (Option A — paired-user model)

### Problem

Before Option A, every AgentHQ agent ran as a single Linux user. Both
Claude itself and the MCP servers it spawned executed under that uid.
Credentials were loaded with systemd's `LoadCredentialEncrypted=` and
ended up readable by the agent process. A prompt injection that pushed
Claude to `cat /run/credentials/agent@alice.service/m365_client_secret`
would succeed — and the cred could then be exfiltrated via any outbound
channel the agent already has (Telegram, WebFetch, etc).

### Fix: two users per agent

For each agent `<name>` there are now **two** Linux users:

| User           | Identity         | What it runs                | Sees creds? |
|----------------|------------------|-----------------------------|-------------|
| `<name>`       | the LLM          | `claude` (untrusted side)   | **No**      |
| `<name>-mcp`   | trusted peer     | every MCP server            | Yes         |

`<name>-mcp` is a system user with no shell, no home directory, and no
login. It exists only as the identity that holds decrypted credentials
behind the MCP tool interface.

### Mechanism

```
[ agent unit ]                       [ agent-mcp-creds unit ]
agent@<name>.service                 agent-mcp-creds@<name>.service
User=<name>                          User=<name>-mcp
ExecStart=claude                     RuntimeDirectory=agents/<name>-mcp/
                                     LoadCredentialEncrypted=… (every cred)
   │                                 ExecStart=agent-mcp-creds-install
   │                                       │
   │ runs claude                           │ copies $CREDENTIALS_DIRECTORY → /run/agents/<name>-mcp/credentials/  (mode 0400)
   │                                       ▼
   │                                  /run/agents/<name>-mcp/credentials/   ← only readable by <name>-mcp (dir mode 0700)
   │
   │ MCP servers from .mcp.json
   ▼
sudo -u <name>-mcp -n /opt/agents/bin/agent-mcp-launcher <server>
   │
   │ (sudoers fragment /etc/sudoers.d/agent-<name> permits ONLY this binary)
   ▼
agent-mcp-launcher runs as <name>-mcp:
  • validates <server> against /home/<name>/agent.toml [tools].enabled
  • exports each cred from /run/agents/<name>-mcp/credentials/ as env
  • exec /opt/agents/tools/<server>/.venv/bin/python server.py
```

The agent process never sees a credential file. The cred only ever
lives in the memory of the trusted-side process behind the MCP tool
interface, and the only operations it exposes are the MCP tool calls
the server defines.

### Components

- **`agent@<name>.service`** — runs Claude. The drop-in `credentials.conf`
  loads only the per-agent telegram bot token (which `agent-prelaunch`
  needs to render `~/.claude/channels/telegram/.env`). All other tool
  creds are no longer here.
- **`agent-mcp-creds@<name>.service`** — `Type=oneshot RemainAfterExit=yes`,
  runs as `<name>-mcp`. Its drop-in loads every tool credential. The
  `RuntimeDirectory=agents/%i-mcp` directive creates `/run/agents/<name>-mcp/`
  owned by `<name>-mcp` mode 0700. `agent-mcp-creds-install` then mirrors
  the decrypted creds into `credentials/` at mode 0400.
  `RuntimeDirectoryPreserve=restart` keeps the dir alive across restarts.
- **`/etc/sudoers.d/agent-<name>`** —
  `<name> ALL=(<name>-mcp) NOPASSWD: /opt/agents/bin/agent-mcp-launcher`.
  Generated by `agent-control` and `visudo -cf` checked before install.
- **`/opt/agents/bin/agent-mcp-launcher`** — the choke point. Identity
  check (`id -un` ends in `-mcp`), allowlist check against `agent.toml`,
  cred export, then `exec` the MCP server. Pattern-validates the
  `<server>` arg so neither `/etc/passwd` nor `../foo` ever gets through.
- **`/home/<name>/.mcp.json`** — every entry's `command` is
  `/usr/bin/sudo` and `args` start with `-u <name>-mcp -n
  /opt/agents/bin/agent-mcp-launcher <server>`. Claude spawns the
  launcher; the launcher spawns the server.

### Telegram bot token: deliberate exception

The telegram bot token stays loaded into the agent unit (so
`agent-prelaunch` can write the plugin's `.env`). A compromised LLM
already controls the bot connection itself — moving the token under
the trusted peer would only stop direct file-reads, not misuse of the
channel — so the additional isolation cost wasn't worth it.

### Group membership requirement

`<name>-mcp` is added to `<name>`'s user-private group at create time
(`useradd … --groups agents,<name>`) so it can read
`/home/<name>/agent.toml` (mode 0644 inside a 0750 home directory).
This trust direction is fine — `<name>-mcp` is the trusted peer; it
already holds every tool credential. The reverse — agent reading
trusted-side state — is what the design blocks.

### Known limitation (out of scope for this PR)

The launcher reads the allowlist from `/home/<name>/agent.toml`, which
is owned by `<name>`. A maximally-compromised LLM could rewrite its own
`agent.toml` to enable additional installed tools. It still cannot
**read** any cred file (the goal of Option A), and `/opt/agents/tools/`
is root-owned so it can't drop a malicious server, but it could call
the MCP interface of any installed tool. A follow-up PR should move the
allowlist to a root-owned path that the agent uid cannot modify.

### Migration

`scripts/migrate-to-paired-users <name>` converts an existing single-user
agent in place. Idempotent. Use after upgrading the box; new agents
created via `agent-control create` are paired by default.

## Open questions

- **TPM2 firmware gate.** Agents-01 currently runs Infineon SLB9670
  FW 7.62 (CVE-2025-2884). Bootstrap should detect FW version and
  refuse TPM2 mode below 7.86, falling back to host-key.
- **agent-control** — port the existing prototype at
  `/home/alice/agent-control/` or rewrite cleanly inside AgentHQ?
- **Programmatic connector disabling** — is there a claude-code
  setting that disables account-level connectors per agent? If yes,
  AgentHQ should set it on every agent template by default.
