# Microsoft 365 — setup

Wires AgentHQ into your Microsoft 365 tenant via Microsoft Graph
delegated auth. Roughly 5 minutes if you have admin rights on the
tenant; longer if a separate IT admin needs to grant consent.

## 1. Register an app in Azure AD

1. Go to <https://portal.azure.com>
2. **Microsoft Entra ID** → **App registrations** → **New registration**
3. Name: `AgentHQ` (or whatever — internal label only)
4. Supported account types: **Accounts in this organizational directory only** (single tenant)
5. Redirect URI: leave blank for now (we use device flow for sign-in)
6. Click **Register**

On the app's Overview page note:
- **Application (client) ID** — paste this when AgentHQ asks for `client_id`
- **Directory (tenant) ID** — paste this when AgentHQ asks for `tenant_id`

## 2. Grant Graph API permissions

App overview → **API permissions** → **Add a permission** →
**Microsoft Graph** → **Delegated permissions**. Add each of:

| Permission | What it lets the agent do |
|---|---|
| `Mail.ReadWrite` | Read, draft, archive, delete emails |
| `Mail.Send` | Send email *(only if you grant `outlook_email_send`)* |
| `Calendars.ReadWrite` | View and manage calendar events |
| `Files.ReadWrite.All` | Search, read, write OneDrive + SharePoint files |
| `User.Read` | Identify the signed-in user |
| `offline_access` | Allow the refresh token (long-lived sessions) |

After adding, click **Grant admin consent for [your tenant]** at the
top. The status column should turn green for every permission.

## 3. Create a client secret

App overview → **Certificates & secrets** → **Client secrets** →
**New client secret**.

- Description: `AgentHQ`
- Expiry: 24 months (or whatever your policy allows — note the renewal)

Copy the **Value** column (NOT the Secret ID — those are different
fields). Paste it when AgentHQ asks for `client_secret`. You can't
retrieve it later, only generate a new one.

## 4. Activate in AgentHQ

When you `agent-control integrations enable m365` (CLI) or click
**Enable** in the Integrations tab, you'll be prompted for:

- `m365_tenant_id` — from step 1
- `m365_client_id` — from step 1
- `m365_client_secret` — from step 3

AgentHQ encrypts each into the systemd-creds vault and runs a
device-flow sign-in to get a refresh token. The first time, you'll
see a code + URL — open the URL on any device, sign in, paste the
code, and you're done. Subsequent calls auto-refresh silently.

## 5. Per-agent grants

Once activated, every agent can be granted access in the Permissions
tab. Default is no access. Tick the specific tools the agent needs:

- A research-y agent: `outlook_email_search`, `calendar_search`, `onedrive_search` (read-only)
- An EA agent: add `outlook_email_draft`, `outlook_email_archive`, `calendar_create_event`
- Send-on-your-behalf: `outlook_email_send` (marked destructive — confirm before granting)

## Troubleshooting

- **"AADSTS50173: The provided grant has expired"** — your refresh
  token was revoked (admin policy, password change, MFA reset).
  Re-run the device-flow sign-in via `agent-control integrations refresh m365`.
- **"Insufficient privileges"** — a permission you're calling wasn't
  added in step 2. Check API permissions, ensure admin consent is granted.
- **"AADSTS500011"** — wrong tenant_id, or you registered the app
  in a different tenant than where your mailbox lives.
