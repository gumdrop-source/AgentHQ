# Xero — setup

Wires AgentHQ into Xero accounting. Read-only by design — this app
registration intentionally requests only `*.read` scopes.

**Per-user authentication, explicit organisation pick:** every Telegram
user signs in to Xero once with their own account, then explicitly tells
the bot which connected organisation queries should run against (even
if there's only one connected — a deliberate friction so a user with
both Gigafy's books and a client's never accidentally answers a
"what's our P&L?" question against the wrong file).

Roughly 5 minutes of one-time admin setup, plus ~45 seconds for each
user the first time they use the bot (sign in, pick an org).

## 1. Register an app in Xero (admin, one time)

1. Go to <https://developer.xero.com/myapps>
2. **New app**:
   - **App name** — anything internal (e.g. `AgentHQ`)
   - **Integration type** — *Web app* (works fine; *Mobile or desktop
     app* also fine)
   - **Company or application URL** — anything; not validated for OAuth
   - **OAuth 2.0 redirect URI** — `http://localhost`  (must match
     exactly, no trailing slash, no path)
3. After registration, open your app → **Configuration** →
   - Note the **Client id**
   - Click **Generate a secret** → copy the **VALUE** (Xero shows it
     once; if you lose it, generate a new one)
4. Back in your app overview, you can leave the scopes blank — the
   tool requests scopes by name in the authorize URL, not from the
   app config.

The full scope string the tool requests is:

```
offline_access accounting.contacts accounting.transactions accounting.settings accounting.reports.read
```

These are the **broad** scopes — they grant read AND write capability
on contacts/transactions/settings. The tool itself only ever makes
GET calls (read-only by code), but the underlying token could in
principle write. We use the broad scopes because the granular `.read`
flavors (`accounting.contacts.read` etc.) require an opt-in that
fresh Web-app registrations don't get by default — requesting them
yields `unauthorized_client / Invalid scope for client` at Xero's
authorize endpoint.

## 2. Activate the integration in AgentHQ (admin, one time)

Open the wizard at `http://<host>:5000`, sign in, navigate to
**Integrations → Xero Accounting → Activate**. Fill:

- **Client ID** — from step 1
- **Client Secret** — from step 1

That's it. No admin OAuth dance is needed. Each Telegram user runs
their own dance via the bot.

(If you want a platform-level token for direct CLI/harness access, set
`xero_refresh_token` manually via `sudo agenthq-cred set
xero_refresh_token` after running the dance once yourself.)

## 3. Grant Xero tools to an agent

Agents page → pick the agent → **Permissions** → tick the Xero tools
the agent should be allowed to call. Save. The wizard regenerates the
agent's `agent.toml`, `.mcp.json`, and the systemd cred drop-in, then
restarts the relevant services.

For most use cases you want all of: `xero_complete_auth`,
`xero_who_am_i`, `xero_organisations`, `xero_set_active_org`,
`xero_organisation_info`, plus whichever data tools you need
(`xero_contacts`, `xero_invoices`, `xero_bills`, `xero_accounts`,
`xero_pl`).

## 4. First-time per-user dance (each user, ~45 seconds)

The first time a Telegram user asks the agent something Xero-related
(e.g. *"what bills are outstanding?"*), the bot replies with:

> To do that I need access to your Xero account. Sign in here:
> https://login.xero.com/identity/connect/authorize?client_id=…
>
> After you sign in (and pick which Xero organisations to share),
> Xero will redirect to a "site can't be reached" page on
> http://localhost — that's expected. Copy the entire URL from your
> browser's address bar and send it back to me, then ask your
> question again.

The user clicks the link, signs in to Xero with their own account,
ticks which organisation(s) the AgentHQ app should access on the
consent screen, sees the expected `localhost` failure page, copies
the URL from their browser's address bar (`Ctrl+L`, `Ctrl+C`),
pastes it as a reply to the bot. The bot calls `xero_complete_auth`,
the per-user refresh token gets stored at:

```
/var/lib/agents/<agent>-mcp/xero_tokens/<chat_id>.json   (mode 0600)
```

Then the user asks their question again. This time the bot prompts:

> Which Xero organisation should I run this against?
>   1. Gigafy Pty Ltd  (tenant_id: abc-123-…)
>   2. Acme Client Pty Ltd  (tenant_id: def-456-…)
>
> Reply with the name (or number) and I'll lock it in.

The user picks; the bot calls `xero_set_active_org`; the active org
is stored at:

```
/var/lib/agents/<agent>-mcp/xero_active_org/<chat_id>.json   (mode 0600)
```

Subsequent queries from that user run silently against the picked org.

To switch orgs: ask the bot to "switch to ..." or "use ...". It will
call `xero_set_active_org` with the new tenant_id.

## 5. Diagnostics

- `xero_who_am_i` — has the calling user linked, and what's their
  active org?
- `xero_organisations` — which orgs is the linked user connected to?
- `xero_organisation_info` — sanity check that the picked org is
  reachable (returns legal name, base currency, financial year end).

## What sees what

Each user only sees data the orgs they've consented to share. Within
those orgs they see what their Xero role permits (Standard, Adviser,
etc.). This is enforced by Xero at the API layer; AgentHQ just
forwards each user's bearer token + the picked tenant_id and surfaces
what comes back.

**Trust boundary caveat:** the MCP tool determines "which user is
asking" by reading the Telegram `chat_id` parameter that the LLM
passes on each tool call. Soft trust — same caveat as MYOB / Tanda.
See `tools/myob/setup.md` for the longer note.

## Troubleshooting

- **`invalid_grant` on refresh** — Xero refresh tokens are single-use
  and rotate on every refresh. The chain has been broken (something
  else used the token, the user revoked the app, or the refresh
  token has been idle for >30 days). The tool detects this, deletes
  the stale token + active-org files, and prompts the user to
  re-authorize on the next call.
- **`unauthorized_client / Invalid scope for client`** at the Xero
  sign-in page — at least one requested scope isn't enabled for your
  app. The tool ships the broad scopes by default (which work for
  every Web-app registration); if you've customised `OAUTH_SCOPE` to
  use granular `.read` flavors and hit this, revert to the broad
  scopes or contact Xero support to enable the granular set on your
  app.
- **`org_required` payload returned even though the user just picked
  one** — the user's previously-picked tenant is no longer in
  `/connections` (org owner revoked the connection in Xero settings).
  The tool clears the stale pick and re-prompts. Have the user pick
  again from the new list.
- **`401` on every call** — system-clock drift can cause Xero to
  reject fresh access tokens. The tool retries once automatically;
  persistent 401s mean the host clock is wrong.
- **`AuthenticationUnsuccessful` from /Accounts or /Invoices** — the
  scope set didn't include the relevant `*.read` scope, or the user's
  Xero role doesn't grant access. Check the consent screen the user
  saw and confirm all 4 read scopes were ticked.
- **`redirect_uri_mismatch` on token exchange** — the URI registered
  with the Xero app must match exactly. The tool always sends
  `http://localhost`. Edit the app at developer.xero.com/myapps and
  fix it (no trailing slash, no path, no port).

## Multi-org workflow notes

- Each user keeps their own active-org pick; switching orgs is per-user.
- Refreshing a user's token does NOT invalidate their active-org pick
  unless the connection itself has been revoked.
- The picker fires whenever the active-org file is missing OR the
  picked tenant is no longer in `/connections` (so revoking a
  connection on the Xero side cleanly forces a re-pick on the next
  query).

## Migration from single-account mode

The `xero_refresh_token` admin field is hidden in the wizard but
honoured if set in the vault — same fallback pattern as MYOB / Tanda.
To retire it after switching to per-user:

1. Delete `/var/lib/agents/<agent>-mcp/xero_refresh_token` (if present)
2. `sudo rm /etc/agents/credentials/xero_refresh_token.cred` (if present)
3. Restart `agent-mcp-creds@<agent>.service` and
   `agent@<agent>.service`

After that, every user — including the admin — must run the per-user
dance through Telegram.
