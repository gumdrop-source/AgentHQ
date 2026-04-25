# Tools

Each subdirectory here is a self-contained MCP server. Bootstrap
phase 30 syncs them to `/opt/agents/tools/<name>/` and installs
their dependencies.

## Contract

A tool directory must contain:

- `server.py` (Python) **or** `server.ts` (TypeScript) — the MCP entry point
- `requirements.txt` (if Python) or `package.json` (if Node)
- `tool.toml` — metadata: name, version, description, required credentials
- `README.md` — what this tool does, what credentials it needs

A tool **must not**:

- Hard-code credentials, API keys, or tenant-specific identifiers
- Depend on the existence of any specific agent's home directory
- Write outside `$AGENT_HOME` or `/var/log/agents/`

Credentials are passed in via the systemd `LoadCredentialEncrypted=`
mechanism — the tool reads from `$CREDENTIALS_DIRECTORY/<cred-name>`.

## Planned tools

- `m365` — Microsoft 365 (mail, calendar, contacts, files, Teams)
- `gmail` — Gmail IMAP/SMTP
- `myob` — MYOB AccountRight (read-only)
- `xero` — Xero accounting
- `homeassistant` — Home Assistant (lights, climate, locks, …)
- `hikvision` — Hikvision NVR (snapshots, PTZ, motion events)
- `paypal` — PayPal Transaction Search API
- `vapi` — Vapi voice assistant control plane
- `cloudflare` — Cloudflare DNS / tunnel management

(Currently empty — porting from `/home/alice/bridges/` happens once
the bootstrap is proven on the test box.)
