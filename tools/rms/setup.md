# RMS Cloud — setup

Wires AgentHQ into RMS Cloud's REST API for read-only revenue and
reservation queries against an RMS-managed property (e.g. Halcyon
House). **Single shared service-account auth** — admin pastes the
agent + client credentials once, no per-user OAuth dance.

## 1. Become an RMS API partner (one time, billable)

If you're not already an approved partner, this step is gated by RMS:

- Email **apisupport@rmscloud.com** (or fill the form at
  sevenrooms.com/partnership-opportunities/) with: mutual-customer
  confirmation (the property owner approves the integration), a
  description of what you're building, and a technical contact.
- Pay the **AUD $550 one-time API Developer Kit fee** — covers your
  agent credentials, sandbox access, a 1h kickoff call, and dev/test
  support.
- The property pays an ongoing monthly module fee on their RMS
  subscription (Halcyon currently has the **Revenue Management** module
  at AUD $49/month — sufficient for revenue/reservation reads).

You'll receive (by email + Bitwarden Send):
- **Agent ID** (numeric — Gigafy is `1249`)
- **Agent password**
- Sandbox / production REST API URLs
- Partner Portal access

The customer (property operator) separately gives you:
- **Client ID** (numeric — Halcyon House is `23267`)
- **Web Service password** (this is RMS-specific terminology — when
  asking the operator for it, use exactly that wording)

## 2. Activate the integration in AgentHQ (admin, one time)

Wizard at `http://<host>:5000` → **Integrations → RMS Cloud (PMS) →
Activate**. Fill:

- **Agent ID** — e.g. `1249`
- **Agent password** — from the Partner Welcome email
- **Client ID** — e.g. `23267` for Halcyon House
- **Web Service password** — from the property operator
- **Module type** — defaults to `revenueManagement`. Leave as-is unless
  the property's RMS subscription has a different API module enabled.
- **Use training database (sandbox)** — set to `true` for first-pass
  testing against RMS's SBX environment, then flip to blank/false once
  you've verified queries against real production data.
- **API base URL override** — leave blank. The tool calls
  `/clienturl/<rmsClientId>` to discover which regional server (e.g.
  `restapi8.rmscloud.com`) your client is pinned to. Only fill if you
  need to force a specific server (e.g. `betarestapi8.rmscloud.com`
  for sandbox).

That's it — no admin OAuth dance. The server.py module caches the
24-hour authToken in process memory and re-mints it transparently when
it expires.

## 3. Grant RMS tools to an agent

Agents page → pick the agent → **Permissions** → tick the RMS tools
the agent should be allowed to call. Save. The wizard regenerates the
agent's `agent.toml`, `.mcp.json`, and the systemd cred drop-in, then
restarts the relevant services.

For the headline "what was last week's revenue?" workflow, the agent
needs at minimum: `rms_who_am_i`, `rms_revenue_summary`. Add
`rms_reservations` and `rms_reservation_revenue` for drill-down.

## 4. First-call check

Have the agent run `rms_who_am_i`. Expected output:

```
{
  "linked": true,
  "agent_id": "1249",
  "client_id": "23267",
  "rms_client_id": 23267,
  "is_multi_property": false,
  "use_training_database": true,
  "base_url": "https://betarestapi8.rmscloud.com",
  "allowed_properties": [
    {"clientId": 23267, "clientName": "Halcyon House"}
  ],
  "token_expires_at": "..."
}
```

If `rms_client_id` differs from `client_id`, you're on a multi-property
database — the rms_client_id is the parent record. Property-specific
queries should pass the property's `clientId` as `propertyId`.

## 5. Diagnostics

- `rms_who_am_i` — fastest sanity check. Forces a fresh token to
  surface auth failures immediately.
- `rms_properties` — list properties accessible to the credentials.

## What the integration sees

Whatever the **Web Service password** scope grants on the property
side, plus the rate limits for the **module type** you requested. The
tool reads only — there are no PUT/POST endpoints wired up except
search and report bodies, which are read-only by RMS's contract.

## Rate limits worth knowing

- `/authToken` — 25 requests/minute. We cache the token 23 hours so
  this should never hit the limit in practice.
- `/reservations/search*` — 60 requests / 10 seconds, 1-minute block.
- `/reports*` — 60 requests/minute, throttled (excess dropped).
- `/reservations/{id}/dailyRevenue` — 30 requests/minute.
  `rms_revenue_summary` uses the batch endpoint
  `/reservations/dailyRevenue/search` instead (50 IDs per call) which
  has no documented per-endpoint limit and is far more efficient.

## Troubleshooting

- **`401 / token invalid`** — the cached token expired and re-auth
  failed. Most likely the agent or client password was rotated.
  Re-paste the credentials in the wizard.
- **`403 / Forbidden`** — the credentials don't have access to the
  endpoint or property. Check that the property's RMS subscription
  includes the module type you requested (default: revenueManagement).
- **`429 / Too Many Requests`** — rate limit hit. The tool implements
  one automatic 401 retry but does NOT retry 429s — give it a minute
  and retry. For high-volume reservation pulls, prefer
  `rms_revenue_summary` over fan-out via `rms_reservation_revenue`.
- **Empty `allowed_properties`** — the credentials are valid at the
  agent level but the client_id doesn't match a property accessible
  to them. Double-check the Client ID with the property operator.
- **Wrong-region base URL** — RMS pins clients to specific regional
  servers. The tool auto-discovers via `/clienturl`, but if you've
  set `RMS_BASE_URL` it'll override. Clear that override or set it
  to the URL the partner welcome email mentioned for SBX.

## Sandbox to production switch

When you're ready to flip from sandbox (training database) to
production:

1. Confirm queries against sandbox return reasonable shapes
   (`rms_who_am_i` shows training database, `rms_revenue_summary`
   over a recent week returns plausible numbers).
2. In the wizard, edit the integration: set **Use training database**
   to blank/false, save.
3. The cached token invalidates on the agent service restart that
   the wizard triggers; next call picks up production credentials.
4. Repeat the `rms_who_am_i` check — `use_training_database` should
   now be `false`.

## References

- [RMS REST API spec (OpenAPI)](https://restapidocs.rmscloud.com/)
- [Postman collection](https://restapidocs.rmscloud.com/postman_collection.json)
- [Schema diagram](https://restapidocs.rmscloud.com/images/rms-schema.png)
- Partner support: support@rmsapi.zendesk.com
