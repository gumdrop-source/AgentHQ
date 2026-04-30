"""Gigafy Management Portal MCP server.

Connects to the Hotspot Login Services REST API at au.api.hotspotlogin.services
(the machine-facing API behind manage.gigafy.com.au). Uses OAuth2 password
grant against a single shared service account — username + password live in
the systemd-creds vault, mint access tokens on demand, no plaintext on disk.

This is a transitional posture. Cameron's planned per-user OAuth + delegation
will replace this with a proper per-Telegram-user model; until then every
agent that has gigafy_portal granted reads and queries against the same
service account, and the Telegram allowlist is the access boundary.
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP

# ─── credentials ────────────────────────────────────────────────────────────

API_URL = os.environ.get("PORTAL_API_URL", "https://au.api.hotspotlogin.services")
API_USER = os.environ.get("PORTAL_API_USER", "")
API_PASS = os.environ.get("PORTAL_API_PASS", "")
RESELLER_ID = os.environ.get("PORTAL_RESELLER_ID", "")

# Refresh ~30s before the API's stated expiry, to cover clock skew + the
# round-trip latency of any in-flight requests.
ACCESS_TOKEN_REFRESH_MARGIN_SECONDS = 30


def _require_creds() -> None:
    missing = [
        name
        for name, val in [
            ("PORTAL_API_USER", API_USER),
            ("PORTAL_API_PASS", API_PASS),
            ("PORTAL_RESELLER_ID", RESELLER_ID),
        ]
        if not val
    ]
    if missing:
        raise RuntimeError(
            f"Gigafy Portal credentials missing: {', '.join(missing)}. "
            "Activate the gigafy_portal integration in agent-control "
            "(or run `sudo agenthq-cred set <key>` for each)."
        )


# ─── access-token cache ────────────────────────────────────────────────────

# Single in-process cache + lock around the password grant. The grant is
# cheap (~50 ms) but doing it once per request would be wasteful.
_token_lock = threading.Lock()
_cached_access_token: str | None = None
_cached_access_token_expires_at: float = 0.0


def _refresh_access_token() -> tuple[str, int]:
    """Run the OAuth2 password grant. Returns (access_token, expires_in_seconds)."""
    _require_creds()
    r = requests.post(
        f"{API_URL}/token",
        data={
            "username": API_USER,
            "password": API_PASS,
            "grant_type": "password",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(
            f"Gigafy Portal token grant failed ({r.status_code}): {r.text[:300]}. "
            "Verify portal_api_user / portal_api_pass are correct, and that the "
            "service account hasn't been disabled."
        )
    payload = r.json()
    access = payload.get("access_token")
    if not access:
        raise RuntimeError(f"Token response had no access_token: {payload}")
    return access, int(payload.get("expires_in") or 3600)


def _access_token() -> str:
    global _cached_access_token, _cached_access_token_expires_at
    with _token_lock:
        now = time.time()
        if _cached_access_token and now < _cached_access_token_expires_at:
            return _cached_access_token
        token, expires_in = _refresh_access_token()
        _cached_access_token = token
        _cached_access_token_expires_at = (
            now + max(expires_in - ACCESS_TOKEN_REFRESH_MARGIN_SECONDS, 60)
        )
        return token


# ─── HTTP helpers ──────────────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_access_token()}",
        "Accept": "application/json",
    }


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET against the Portal API. `path` is appended to PORTAL_API_URL."""
    _require_creds()
    if not path.startswith("/"):
        path = "/" + path
    r = requests.get(f"{API_URL}{path}", headers=_headers(), params=params, timeout=30)
    if r.status_code == 401:
        # Stale cached access token (clock skew, server-side invalidation).
        # Force a fresh grant and retry once before giving up.
        global _cached_access_token_expires_at
        with _token_lock:
            _cached_access_token_expires_at = 0.0
        r = requests.get(f"{API_URL}{path}", headers=_headers(), params=params, timeout=30)
    if not r.ok:
        body = r.text[:500] if r.text else ""
        raise RuntimeError(f"Portal GET {path} failed ({r.status_code}): {body}")
    if not r.content:
        return None
    return r.json()


def _post_multipart(path: str, file_path: str, field: str = "files") -> Any:
    """POST a single file to the Portal API as multipart/form-data.

    Used by the attachment upload endpoint, which expects a plain
    multipart upload with field name `files` (the API reads only the
    first content from the multipart envelope). Don't set
    Content-Type — requests fills it with the boundary string.
    """
    _require_creds()
    if not path.startswith("/"):
        path = "/" + path
    p = Path(file_path)
    if not p.exists() or not p.is_file():
        raise ValueError(f"file not found or not a regular file: {file_path}")
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    with p.open("rb") as fh:
        files = {field: (p.name, fh, mime)}
        r = requests.post(f"{API_URL}{path}", headers=_headers(), files=files, timeout=120)
        if r.status_code == 401:
            global _cached_access_token_expires_at
            with _token_lock:
                _cached_access_token_expires_at = 0.0
            fh.seek(0)
            files = {field: (p.name, fh, mime)}
            r = requests.post(f"{API_URL}{path}", headers=_headers(), files=files, timeout=120)
    if not r.ok:
        body_txt = r.text[:500] if r.text else ""
        raise RuntimeError(f"Portal multipart POST {path} failed ({r.status_code}): {body_txt}")
    if not r.content:
        return None
    # Endpoint returns either a JSON value or a bare string (filename).
    try:
        return r.json()
    except ValueError:
        return r.text


def _post(path: str, body: Any | None = None) -> Any:
    """POST against the Portal API with a JSON body."""
    _require_creds()
    if not path.startswith("/"):
        path = "/" + path
    headers = {**_headers(), "Content-Type": "application/json"}
    r = requests.post(f"{API_URL}{path}", headers=headers, json=body, timeout=30)
    if r.status_code == 401:
        global _cached_access_token_expires_at
        with _token_lock:
            _cached_access_token_expires_at = 0.0
        headers = {**_headers(), "Content-Type": "application/json"}
        r = requests.post(f"{API_URL}{path}", headers=headers, json=body, timeout=30)
    if not r.ok:
        body_txt = r.text[:500] if r.text else ""
        raise RuntimeError(f"Portal POST {path} failed ({r.status_code}): {body_txt}")
    if not r.content:
        return None
    return r.json()


def _put(path: str, body: Any | None = None) -> Any:
    """PUT against the Portal API with a JSON body. Used for upsert
    semantics — the API's invoice and supplier "save" endpoints are PUTs
    where the body's entityId discriminates create-vs-update."""
    _require_creds()
    if not path.startswith("/"):
        path = "/" + path
    headers = {**_headers(), "Content-Type": "application/json"}
    r = requests.put(f"{API_URL}{path}", headers=headers, json=body, timeout=30)
    if r.status_code == 401:
        global _cached_access_token_expires_at
        with _token_lock:
            _cached_access_token_expires_at = 0.0
        headers = {**_headers(), "Content-Type": "application/json"}
        r = requests.put(f"{API_URL}{path}", headers=headers, json=body, timeout=30)
    if not r.ok:
        body_txt = r.text[:500] if r.text else ""
        raise RuntimeError(f"Portal PUT {path} failed ({r.status_code}): {body_txt}")
    if not r.content:
        return None
    return r.json()


def _delete(path: str) -> Any:
    """DELETE against the Portal API."""
    _require_creds()
    if not path.startswith("/"):
        path = "/" + path
    r = requests.delete(f"{API_URL}{path}", headers=_headers(), timeout=30)
    if r.status_code == 401:
        global _cached_access_token_expires_at
        with _token_lock:
            _cached_access_token_expires_at = 0.0
        r = requests.delete(f"{API_URL}{path}", headers=_headers(), timeout=30)
    if not r.ok:
        body_txt = r.text[:500] if r.text else ""
        raise RuntimeError(f"Portal DELETE {path} failed ({r.status_code}): {body_txt}")
    if not r.content:
        return None
    return r.json()


# Sentinel "no entity" GUID — used as a path segment for "create new" PUT
# routes (the API uses this rather than a separate /create endpoint).
ZERO_GUID = "00000000-0000-0000-0000-000000000000"


# ─── MCP server ────────────────────────────────────────────────────────────

mcp = FastMCP(
    "gigafy_portal",
    instructions=(
        "Read-only access to the Gigafy Management Portal "
        "(manage.gigafy.com.au, backed by au.api.hotspotlogin.services). "
        "Tools are added incrementally — start with gigafy_portal_ping for "
        "a connectivity sanity check."
    ),
)


@mcp.tool()
def gigafy_portal_ping() -> dict:
    """Connectivity sanity check.

    Mints an access token via the password grant and returns the configured
    API URL + service-account username (without the password) + token TTL.
    Useful for confirming the integration is wired up before any real tools
    are defined.
    """
    _require_creds()
    # Force a fresh grant so we report the actual TTL the API just returned,
    # not the cached value from a previous call.
    with _token_lock:
        global _cached_access_token, _cached_access_token_expires_at
        _cached_access_token = None
        _cached_access_token_expires_at = 0.0
    token = _access_token()
    ttl_remaining = int(_cached_access_token_expires_at - time.time())
    return {
        "ok": True,
        "api_url": API_URL,
        "user": API_USER,
        "reseller_id": RESELLER_ID,
        "access_token_prefix": token[:10] + "…" if token else None,
        "expires_in_seconds": ttl_remaining,
    }


# ─── lookups (research tools the bot uses before composing an invoice) ────

@mcp.tool()
def gigafy_portal_supplier_lookup(query: str) -> Any:
    """Find suppliers by name fragment. Use to resolve a vendor name from
    an invoice into the supplierEntityId the create payload needs.

    Args:
        query: Substring of the supplier name (case-insensitive on the
            server side).
    """
    if not query:
        raise ValueError("query is required")
    return _get(f"/api/Suppliers/{RESELLER_ID}/Lookup", params={"query": query})


@mcp.tool()
def gigafy_portal_stock_lookup(query: str, filter: int = 0) -> Any:
    """Find stock / catalogue items by name or code fragment. Each line
    item on a purchase invoice references one stockEntityId.

    Args:
        query: Substring to search on (name or product code).
        filter: Optional Int16 server-side filter category (default 0).
    """
    if not query:
        raise ValueError("query is required")
    return _get(
        f"/api/Stock/{RESELLER_ID}/Lookup",
        params={"query": query, "filter": filter},
    )


@mcp.tool()
def gigafy_portal_account_lookup(query: str) -> Any:
    """Find chart-of-accounts entries (ledger accounts) by name or code
    fragment. Each invoice line item is coded against one account
    (its coaEntityId / coaCode).

    Args:
        query: Substring of the account name or display ID. For example
            'rent' or '6-' (account-code prefix).
    """
    if not query:
        raise ValueError("query is required")
    return _get(
        f"/api/Resellers/{RESELLER_ID}/Ledger/Accounts/Lookup",
        params={"query": query},
    )


@mcp.tool()
def gigafy_portal_tax_group_list() -> Any:
    """List every tax group configured for the reseller. Each invoice
    line item references one taxGroupEntityId — typically GST or N-T
    (no tax) for Australian customers.

    Returns the full set; no filtering needed since the list is short
    (a handful of groups per reseller).
    """
    return _get(f"/api/Resellers/{RESELLER_ID}/TaxGroups")


@mcp.tool()
def gigafy_portal_work_order_lookup(criteria: str) -> Any:
    """Find work orders / job tickets by free-text search. Optional —
    only set ticketEntityId on a line item if the operator wants to
    attribute the cost to a specific job.

    Note: this endpoint is POST + body=string (not query param).
    """
    if not criteria:
        raise ValueError("criteria is required")
    return _post(f"/api/WorkOrders/{RESELLER_ID}/Search", body=criteria)


# ─── purchase-invoice composition + create ─────────────────────────────────

@mcp.tool()
def gigafy_portal_invoice_blank() -> dict:
    """Get an empty purchase-invoice template for the configured reseller.

    Returns the JSON skeleton the Portal expects for a new invoice —
    pre-allocated entityId, default fields, empty table1/table2 arrays.
    Use as the starting point for composing a create payload.
    """
    return _get(f"/api/Purchases/Invoices/{RESELLER_ID}/Blank")


@mcp.tool()
def gigafy_portal_invoice_history(supplier_entity_id: str, limit: int = 10) -> Any:
    """List recent purchase invoices from one supplier — for
    "learn from prior coding" when filling in a new invoice.

    Returns the matching invoice rows (header summary). Use the entityId
    on a row to drill into a full invoice via gigafy_portal_invoice_load,
    then crib the line item shape (stockEntityId / coaEntityId /
    taxGroupEntityId / taxes JSON) for the new invoice.

    Args:
        supplier_entity_id: GUID of the supplier — get it from
            gigafy_portal_supplier_lookup.
        limit: Max rows to return (1–50, default 10).
    """
    if not supplier_entity_id:
        raise ValueError("supplier_entity_id is required")
    listing = _post(
        f"/api/Purchases/Invoices/{RESELLER_ID}/{ZERO_GUID}",
        body={},
    )
    rows = (listing or {}).get("table", [])
    matches = [r for r in rows if r.get("supplierEntityId") == supplier_entity_id]
    matches.sort(key=lambda r: r.get("purchaseDate") or "", reverse=True)
    return matches[: max(1, min(limit, 50))]


@mcp.tool()
def gigafy_portal_invoice_load(entity_id: str) -> dict:
    """Load one purchase invoice by GUID. Returns the full record
    including table1 (line items) and table2 (attachments).

    Use to inspect a prior invoice's line-item coding before composing
    a new one — read a similar prior invoice from the same supplier
    and copy its stockEntityId / coaEntityId / taxGroupEntityId /
    taxRateEntityId fields.
    """
    if not entity_id:
        raise ValueError("entity_id is required")
    return _get(f"/api/Purchases/Invoices/{entity_id}")


@mcp.tool()
def gigafy_portal_invoice_attach(invoice_entity_id: str, file_path: str) -> Any:
    """Attach a file (PDF / image / doc) to an existing purchase invoice.

    Use after a successful gigafy_portal_create_purchase_invoice to
    upload the source artefact (e.g. the original Telegram image, or
    the PDF the invoice was parsed from). The file lands in Azure blob
    storage and shows in the GMP UI's invoice attachment strip.

    Args:
        invoice_entity_id: GUID of the purchase invoice to attach to
            (the entityId you used / received from create).
        file_path: Absolute path to the file on disk, readable by the
            -mcp user this tool runs as. Telegram-uploaded images
            arrive at /home/<agent>/.claude/channels/telegram/inbox/
            but that directory is NOT readable by the -mcp peer (the
            agent's .claude/ tree is mode 0700). Before calling this
            tool, copy the file into /tmp first with mode 0644:

              cp /home/<agent>/.claude/channels/telegram/inbox/<file>  /tmp/<file>
              chmod 0644 /tmp/<file>

            Then pass `/tmp/<file>` here.

    Returns the server-issued filename (looks like
    `<attachment-entity-id>.<ext>`); the entity-id portion is what you'd
    pass to a delete call.
    """
    if not invoice_entity_id:
        raise ValueError("invoice_entity_id is required")
    if not file_path:
        raise ValueError("file_path is required")
    return _post_multipart(
        f"/api/Purchases/Invoices/{invoice_entity_id}/Attach/{RESELLER_ID}",
        file_path,
    )


@mcp.tool()
def gigafy_portal_create_purchase_invoice(invoice_json: str) -> dict:
    """Create a purchase invoice in the Portal.

    DESTRUCTIVE — writes directly to the live Portal. Always show the
    operator the full JSON preview and get explicit confirmation before
    calling.

    Important: the front-end sends a FLAT header object where line items
    live as a STRINGIFIED JSON ARRAY in `header.items`, not as a separate
    table1 array. Specifically:

      const rows = [/* array of line item objects */];
      header.saleLocation = JSON.stringify(saleLocation);
      header.items        = JSON.stringify(rows);
      PUT /api/Purchases/Invoices    body = header

    Recommended pattern:
      1. supplier_lookup → resolve supplierEntityId
      2. invoice_history(supplier_id) → find a similar prior invoice
      3. invoice_load(prior_id) → read its table1 line items
      4. invoice_blank() → fresh header skeleton (its entityId is
         pre-allocated; use it as parentEntityId on each new line item)
      5. Copy the prior coding fields onto your new line items, swap
         in the new productName / productCost / total / quantity / GST
      6. Build the flat header object: copy from blank's table[0],
         set supplierEntityId, invoiceNumber, invoiceDate, dueDate,
         saleLocation (stringify it), items (stringify the line items)
      7. Show the operator a preview, ask "save?"
      8. On explicit yes, call this tool with the JSON-encoded header

    Args:
        invoice_json: JSON-encoded flat header object. `items` should be
            a JSON-encoded STRING (a stringified array of line items),
            not an array. `saleLocation` likewise stringified.

    Response shape: {item1: bool, item2: string} — Tuple<success, message>.
    """
    try:
        body = json.loads(invoice_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"invoice_json is not valid JSON: {e}") from e
    if not isinstance(body, dict):
        raise ValueError("invoice_json must encode a JSON object")
    return _put("/api/Purchases/Invoices", body)


# ─── entry ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
