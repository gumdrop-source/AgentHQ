# MYOB AccountRight — setup

Wires AgentHQ into a single MYOB AccountRight Live company file via the
MYOB API. Read-only by design — this app registration intentionally has
no write scopes.

Roughly 15 minutes the first time, including the OAuth handshake. After
that, refresh tokens rotate silently and the tool keeps working through
restarts without operator involvement.

## 1. Register a developer app

1. Go to <https://my.myob.com.au/Bd/RegisteredApps.aspx>
2. **Create a new app**:
   - **Application name** — anything, internal label only (e.g. `AgentHQ`)
   - **Redirect URI** — `http://localhost`
   - **Application type** — Desktop / single-page / native
3. After registration, note the **Client ID** and **Client Secret** —
   you'll paste these into AgentHQ's setup wizard.

The required scopes (granular `sme-*` family) are:

```
offline_access openid sme-banking sme-company-file sme-contacts-employee sme-general-ledger sme-payroll
```

For new app registrations these are already enabled. Do **not** include
the legacy `CompanyFile` scope or `sme-reports` — both return
`invalid_scope` against the modern app registration.

## 2. First-time OAuth consent

Once, by hand. AgentHQ doesn't automate this part because MYOB's only
sign-in flow is a redirect-with-code in a real browser session.

### 2a. Authorize URL

Open this in your browser (replace `<CLIENT_ID>`):

```
https://secure.myob.com/oauth2/account/authorize/?client_id=<CLIENT_ID>&redirect_uri=http://localhost&response_type=code&scope=offline_access%20openid%20sme-banking%20sme-company-file%20sme-contacts-employee%20sme-general-ledger%20sme-payroll
```

Sign in with the my.MYOB account that has access to the company file
you want AgentHQ to read. Approve the consent screen.

### 2b. Capture the code

The browser redirects to `http://localhost/?code=<long-string>`. The
page won't load (nothing's listening on localhost — that's fine). Copy
the entire URL or just the `code` parameter from the address bar.

### 2c. Exchange the code for a refresh token

Run this once on the box (or any machine with `curl`):

```sh
curl -s -X POST https://secure.myob.com/oauth2/v1/authorize \
  -d client_id=<CLIENT_ID> \
  -d client_secret=<CLIENT_SECRET> \
  -d redirect_uri=http://localhost \
  -d code=<CODE_FROM_2B> \
  -d grant_type=authorization_code | jq .
```

The response includes both `access_token` and `refresh_token`. Save the
**refresh_token** — that's what you give AgentHQ.

### 2d. Find the company file GUID

```sh
curl -s https://api.myob.com/accountright/ \
  -H "Authorization: Bearer <ACCESS_TOKEN_FROM_2C>" \
  -H "x-myobapi-key: <CLIENT_ID>" \
  -H "x-myobapi-version: v2" \
  -H "Accept: application/json" | jq .
```

Each company file in the response has a `Uri` like
`https://api.myob.com/accountright/<GUID>`. The GUID is what AgentHQ
calls the **business ID**.

## 3. Provision the credentials

```sh
sudo agenthq-cred set myob_client_id        # paste, Ctrl-D
sudo agenthq-cred set myob_client_secret    # paste, Ctrl-D
sudo agenthq-cred set myob_refresh_token    # paste, Ctrl-D
sudo agenthq-cred set myob_business_id      # paste, Ctrl-D
```

Each cred lands in `/etc/agents/credentials/<name>.cred`, encrypted with
systemd-creds (host-bound TPM key — never plaintext on disk).

## 4. Activate per agent

```sh
sudo agent-control create <name> --tools myob,...    # new agent
# or
sudo agent-control update <name> --tools myob,...    # existing
sudo systemctl restart agent-mcp-creds@<name>.service
sudo systemctl restart agent@<name>.service
```

In the Permissions UI, tick the specific MYOB tools the agent should
see. All MYOB tools are read-only — `destructive: true` is reserved for
write operations, of which there are none in this integration.

## 5. Token rotation — what to expect

MYOB rotates the `refresh_token` on **every** refresh-grant call. The
running tool persists the rotated value to:

```
/var/lib/agents/<agent>-mcp/myob_refresh_token   (mode 0600)
```

This file is owned by the per-agent `-mcp` user and lives outside the
agent user's reach (so a prompt-injected Claude can't exfiltrate it).
The encrypted seed at `/etc/agents/credentials/myob_refresh_token.cred`
is only consulted at first onboarding — once a successful refresh has
happened, the writable cache takes over.

If the refresh ever fails with `invalid_grant`, the chain is broken
(another client used the same token, or the user changed password).
Recovery is: redo step 2 to get a fresh refresh_token, then re-run
`sudo agenthq-cred set myob_refresh_token` and remove the stale cache:

```sh
sudo rm /var/lib/agents/<agent>-mcp/myob_refresh_token
sudo systemctl restart agent-mcp-creds@<agent>.service agent@<agent>.service
```

## Troubleshooting

- **`invalid_scope`** on the authorize URL — you included `sme-reports`
  or legacy `CompanyFile`. Use only the scope set in section 1.
- **`invalid_grant`** on refresh — see "Token rotation" above. The
  refresh token has been used by another client or revoked.
- **`InefficientFilter`** in employee lookups — the underlying OData
  filter doesn't support `contains()`. Use exact `eq` matches via
  `myob_employee_lookup(first_name=…, last_name=…)`.
- **`401 Unauthorized` on every call** — the access-token cache is
  stale (clock skew, MYOB invalidated early). The tool retries once
  automatically; if you see persistent 401s, check the system clock.
