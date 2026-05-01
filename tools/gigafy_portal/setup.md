# Gigafy Management Portal — setup

Connects AgentHQ to the Hotspot Login Services API at
`au.api.hotspotlogin.services` — the machine-facing API behind the
`manage.gigafy.com.au` UI.

**Auth posture (transitional):** a single shared service account, OAuth2
password grant. Username + password live in the systemd-creds vault,
encrypted at rest. The MCP server mints fresh access tokens on demand.

This is intended as a stopgap until the Portal exposes an
authorization-code OAuth flow with delegation — at which point we can
swap to per-user tokens like the new MYOB integration. Until then,
every Telegram user who has gigafy_portal granted reads through the
same service account, and the Telegram allowlist is the access boundary.

## 1. Provision a service account in the Portal

Use a dedicated service account, not a person's login:

- Pick a username like `agenthq` or `mcp-bot`.
- Set a strong, machine-only password.
- Grant the account the read scopes AgentHQ should have (sales orders,
  quotes, BOM — minimum the agent needs).

Avoid using a real human's credentials — they have personal scope creep,
2FA you can't automate around, and password changes that break us
silently.

## 2. Activate the integration in the wizard

Open `http://<host>:5000`, sign in, navigate to
**Integrations → Gigafy Management Portal → Activate**. Fill:

- **Service account username** — from step 1
- **Service account password** — from step 1
- **Reseller GUID** — Gigafy's reseller GUID (ask Cameron / look up in
  the Portal admin pages)
- **API URL** — leave blank unless you're on staging

Click **Activate**. The wizard encrypts each into the systemd-creds vault.

## 3. Grant gigafy_portal tools to an agent

Agents page → pick the agent → **Permissions** → tick the gigafy_portal
tools. Save. The wizard regenerates the agent's `agent.toml`,
`.mcp.json`, and the systemd cred drop-in, then restarts the relevant
services.

## 4. Verify

Ask the agent in Telegram: *"ping the gigafy portal"*. The bot should
call `gigafy_portal_ping` and reply with the connected username + token
TTL. If it errors, check `journalctl -u agent@<name>.service` for the
specific failure.

## What's stored, where

```
/etc/agents/credentials/portal_api_user.cred       # encrypted at rest
/etc/agents/credentials/portal_api_pass.cred       # encrypted at rest
/etc/agents/credentials/portal_reseller_id.cred    # encrypted at rest
/etc/agents/credentials/portal_api_url.cred        # encrypted at rest (only if you set it)
```

systemd-creds binds each to the host's TPM2 key, so the encrypted blobs
are useless if copied off the box. At runtime they're decrypted into a
tmpfs at `/run/agents/<agent>-mcp/credentials/` and exported as env vars
to the MCP server (which runs as the trusted `-mcp` peer, never as the
agent user that holds the LLM).

## Rotating credentials

```sh
sudo agenthq-cred set portal_api_pass
# paste new password, Ctrl-D
sudo systemctl restart agent-mcp-creds@<agent>.service agent@<agent>.service
```

The in-process token cache is wiped by the restart; the next call mints
a token using the new password.

## Migration to per-user OAuth (future)

When Cameron lands the authorization-code flow with delegation, this
integration will be reshaped to mirror the MYOB per-user pattern: each
Telegram user signs in once with their own Portal credentials, gets
their own refresh token, and queries return only what their account is
scoped for. The shared-service-account creds in the vault will be
removed at that point.
