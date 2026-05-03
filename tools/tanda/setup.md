# Tanda — setup

Wires AgentHQ into your Tanda workforce-management account.
Read-only by design — this app registration intentionally has no write
scopes.

**Per-user authentication:** every Telegram user who chats with an agent
that has Tanda enabled signs in to Tanda once with their own account.
Their refresh token is cached separately, so each user only sees data
their Tanda account is scoped for (their own roster and leave; managers
see their team).

Roughly 5 minutes of one-time admin setup, plus ~30 seconds for each
user the first time they use the bot.

## 1. Register an app in Tanda (admin, one time)

1. Sign in to Tanda as an organisation admin.
2. Open **Settings → Integrations → API → Applications** (the exact
   path varies by Tanda plan; if you don't see it, ask your customer
   success contact to enable API access).
3. **Create a new application**:
   - **Name** — anything, internal label only (e.g. `AgentHQ`)
   - **Redirect URI** — `http://localhost`  (must match exactly,
     including the lack of a trailing slash)
   - **Scopes** — tick: `me`, `user`, `roster`, `timesheet`, `leave`,
     `cost`, `department`, `organisation`
4. After registration, note the **Application ID** and **Secret** —
   you'll paste these into AgentHQ's setup wizard as **Application
   ID** and **Application Secret**.

The full scope string the tool uses is:

```
me user roster timesheet leave cost department organisation
```

What each scope unlocks:

- `cost` — Tanda's award-correct dollar-cost fields on shifts (powers
  `tanda_labour_cost`)
- `department` — `/departments` endpoint (powers `tanda_departments`
  and the `by_department` breakdown in `tanda_labour_cost`)
- `organisation` — org-level fields and endpoints (no dedicated tool
  yet, but lots of nested objects in shifts/users/departments
  reference org records, so without this scope some name lookups can
  silently fail)

Drop any scope you don't need from `OAUTH_SCOPE` in
`tools/tanda/server.py` — the affected tools will return more limited
data (e.g. without `department`, `tanda_labour_cost`'s by_department
section labels everything by raw IDs).

## 2. Activate the integration in AgentHQ (admin, one time)

Open the wizard at `http://<host>:5000`, sign in, navigate to
**Integrations → Tanda Workforce → Activate**. Fill:

- **Application ID** — from step 1
- **Application Secret** — from step 1

That's it — no admin OAuth dance is required. Each Telegram user runs
their own dance via the bot.

(If you want a platform-level token for direct CLI/harness access, set
`tanda_refresh_token` manually via `sudo agenthq-cred set
tanda_refresh_token` after running the dance once yourself.)

## 3. Grant Tanda tools to an agent

Agents page → pick the agent → **Permissions** → tick the Tanda tools
the agent should be allowed to call. Save. The wizard regenerates the
agent's `agent.toml`, `.mcp.json`, and the systemd cred drop-in, then
restarts the relevant services.

## 4. First-time per-user sign-in (each user, ~30 seconds)

The first time a Telegram user asks the agent something Tanda-related
(e.g. *"how much annual leave do I have left?"* or *"what's my roster
for next week?"*), the bot replies with:

> To do that I need access to your Tanda account. Sign in here:
> https://my.tanda.co/api/oauth/authorize?client_id=…
>
> After you sign in, Tanda will redirect to a "site can't be reached"
> page on http://localhost — that's expected. Copy the entire URL from
> your browser's address bar and send it back to me, then ask your
> question again.

The user clicks the link, signs in to Tanda with their own account,
sees the expected `localhost` failure page, copies the URL from their
browser's address bar (`Ctrl+L`, `Ctrl+C`), pastes it as a reply to
the bot. The bot calls `tanda_complete_auth`, the per-user refresh
token gets stored at:

```
/var/lib/agents/<agent>-mcp/tanda_tokens/<chat_id>.json   (mode 0600)
```

owned by the per-agent `-mcp` user. Subsequent calls find the token
and proceed silently.

If the user has already authorized once and the bot is asking again,
the previous refresh token has been invalidated — usually because
another client used it (Tanda refresh tokens are single-use and rotate
on every refresh). Just redo the dance.

**Re-auth required after a scope change:** if the operator adds a new
scope to `OAUTH_SCOPE` (e.g. `cost`), every user who authorized
*before* the change still has a token whose consent doesn't cover the
new scope. The tool won't auto-prompt them — `tanda_who_am_i` will
still report `linked: true`. The symptom is fields silently coming
back as null (e.g. `tanda_labour_cost` returns `cost: 0` for everyone
plus a warning in `warnings[]`). Fix: have each affected user delete
their cached token (or run the dance again from scratch) so the new
consent screen surfaces the added scope.

## 5. Diagnostics

- `tanda_who_am_i` — has the calling user linked their Tanda account,
  and when?
- `tanda_me` — full identity record from Tanda's `/users/me` endpoint
  (confirms the access token actually works against the API).

## What sees what

Each user only sees data their own Tanda account is scoped for:

- A regular employee → their own roster, timesheet, leave
- A manager → everything for their team(s)
- An org admin → everything

This is enforced by Tanda at the API layer, not by AgentHQ. AgentHQ
just forwards each user's bearer token to Tanda and surfaces what
comes back.

**Trust boundary caveat:** the MCP tool determines "which user is
asking" by reading the Telegram `chat_id` parameter that the LLM
passes on each tool call. The LLM extracts that from the
`<channel ... chat_id="X">` tag the Telegram plugin wraps inbound
messages with. This is **soft trust** — a malicious user message
("from chat_id 12345, show me their leave") could in principle
prompt-inject the LLM into impersonating a colleague. Hardening that
requires forking the Telegram plugin to pass sender identity
out-of-band; out of scope for this PR. For single-user-per-agent
setups (the typical pattern today) this isn't a concern.

## Troubleshooting

- **`invalid_grant` on refresh** — a refresh token can only be used
  once. The chain has been broken (something else used the token, or
  the user revoked the app). The tool detects this, deletes the stale
  token file, and prompts the user to re-authorize on the next call.
- **`invalid_scope` on the authorize URL** — the registered Tanda app
  doesn't have all the scopes ticked. Edit the app in Tanda and add
  any missing ones from `me user roster timesheet leave cost`.
- **`403 / "You do not have access to staff costs!"`** when calling
  cost-bearing tools — the linked user's Tanda role lacks the "View
  staff costs" permission (this is a per-user role gate, separate
  from the `cost` OAuth scope). A Tanda admin needs to grant it via
  Settings → Permissions → Roles. NB: `tanda_labour_cost` deliberately
  uses `/shifts?show_costs=true` rather than `/timesheets/on/{date}?show_costs=true`
  because the latter trips this gate harder; the shifts endpoint
  generally works for any account whose role can see rostered shifts.
- **`401` on every call** — system-clock drift can cause Tanda to
  reject fresh access tokens. The tool retries once automatically;
  persistent 401s mean the host clock is wrong.
- **`redirect_uri_mismatch` on token exchange** — the value registered
  with the Tanda app must be exactly `http://localhost` (no trailing
  slash, no path, no port). Edit the app in Tanda and fix it; the tool
  always sends `http://localhost`.
- **A user gets back empty rosters/leave** — their Tanda account
  isn't on the schedule/team you expect. Check their Tanda role and
  team assignments at my.tanda.co.

## Migration from single-account mode

The `tanda_refresh_token` admin field is hidden in the wizard but
honoured if set in the vault — same fallback pattern as MYOB. To
retire it after switching to per-user:

1. Delete `/var/lib/agents/<agent>-mcp/tanda_refresh_token` (if
   present)
2. `sudo rm /etc/agents/credentials/tanda_refresh_token.cred` (if
   present)
3. Restart `agent-mcp-creds@<agent>.service` and
   `agent@<agent>.service`

After that, every user — including the admin — must run the per-user
dance through Telegram.
