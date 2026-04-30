# Tool manifest format

Each MCP server under `tools/<name>/` declares itself in a
`tool.json` file. AgentHQ reads these to populate the integrations
catalog, the credential setup wizard, and the per-agent permission
matrix.

## Schema

```json
{
  "name": "myob",
  "title": "MYOB AccountRight",
  "description": "Read access to MYOB AccountRight company file.",
  "credentials": [
    {
      "key": "myob_client_id",
      "label": "OAuth client ID",
      "description": "From your MYOB API portal app registration"
    },
    {
      "key": "myob_client_secret",
      "label": "OAuth client secret",
      "secret": true
    },
    {
      "key": "myob_refresh_token",
      "label": "OAuth refresh token",
      "secret": true,
      "description": "Long-lived token, rotated automatically"
    }
  ],
  "tools": {
    "accounts": {
      "description": "List the chart of accounts"
    },
    "bank_balance": {
      "description": "Read current balance for each bank account"
    },
    "reconciliation": {
      "description": "Manage bank reconciliation entries",
      "destructive": true
    }
  }
}
```

## Field reference

- **`name`** (required) — the MCP server's namespace. Tools inside it
  appear as `mcp__<name>__<tool_name>` in claude's permission list.
- **`title`** — human-readable name for the integrations catalog UI.
- **`description`** — one-sentence summary.
- **`credentials`** — array of credentials this tool requires. Each
  entry describes one credential the user pastes in the integration
  setup wizard. Stored encrypted in the systemd-creds vault.
- **`tools`** — map of individual tool names this MCP server exposes.
  Each tool gets its own row in the per-agent permission matrix, so
  granting `accounts` ≠ granting `reconciliation`.

## Special cases

- **`destructive: true`** (on a tool entry) — the permission matrix UI
  shows this tool with a warning icon and requires explicit confirmation
  when granting.
- **`optional: true`** (on a credential entry) — the wizard renders
  the field without `required` and accepts blank submission. Drop-in
  generators only emit `LoadCredentialEncrypted` for optional creds
  whose `.cred` file actually exists in the vault, so a missing
  optional cred doesn't block the unit from starting.
- **`hidden: true`** (on a credential entry) — the wizard doesn't
  render the field at all (implies `optional: true`). Used for
  credentials operators shouldn't have to think about during
  activation — e.g. `myob_refresh_token`, which is provisioned
  per-user through the agent's auth dance, not the wizard. Admins
  who genuinely want to set a hidden cred can do so via
  `sudo agenthq-cred set <key>`.
- **`oauth` block** — when present, the activate page renders an
  inline helper that walks the operator through an OAuth
  authorization-code flow with optional auto-discovery. See myob's
  manifest for the field shape.
- **Plugin-exposed tools** (like `telegram`, `claude-mem`) don't live
  under `tools/`. They're treated as built-in and always granted.

## Why JSON not TOML

Easier to parse from bash (`jq`) and from TypeScript (`JSON.parse`)
without an extra dependency. Trade-off is slightly less ergonomic
hand-editing, but tool.json files are written once per integration,
not edited often.
