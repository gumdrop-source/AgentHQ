"""RMS Cloud (PMS) MCP server — read-only, single shared service-account auth.

Architecturally similar to gigafy_portal: one set of platform-level
credentials (Agent ID + password from RMS, Client ID + password from
the property), shared across all callers. No per-user OAuth dance.

Auth lifecycle:
  1. /clienturl/{rmsClientId} (no auth) → returns the client-pinned
     base URL. Cached in-process; refreshed on token refresh.
  2. /authToken (no auth) → exchanges agent + client creds for a
     bearer-style token valid 24 hours. Returns the rmsClientId and
     allowedProperties[].
  3. Subsequent requests carry header `authtoken: <token>` (lowercase,
     not 'Authorization', not 'Bearer' — RMS's spec is specific).

We cache the token for ~23 hours. If a 401 comes back mid-life, we
invalidate and re-auth once before giving up.

Reference: https://restapidocs.rmscloud.com/
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP

# ─── credentials ────────────────────────────────────────────────────────────

AGENT_ID = os.environ.get("RMS_AGENT_ID", "")
AGENT_PASSWORD = os.environ.get("RMS_AGENT_PASSWORD", "")
CLIENT_ID = os.environ.get("RMS_CLIENT_ID", "")
CLIENT_PASSWORD = os.environ.get("RMS_CLIENT_PASSWORD", "")
MODULE_TYPE = os.environ.get("RMS_MODULE_TYPE", "revenueManagement")
USE_TRAINING = (os.environ.get("RMS_USE_TRAINING", "").strip().lower()
                in {"1", "true", "yes", "y", "on"})
BASE_URL_OVERRIDE = os.environ.get("RMS_BASE_URL", "").strip().rstrip("/")

# Default seed URL used to call /clienturl when no override is set. Any
# regional production server works for /clienturl since it returns the
# client-specific URL based on rmsClientId; we pick AU since Halcyon is AU.
SEED_BASE_URL = "https://restapi8.rmscloud.com"

ACCESS_TOKEN_TTL_SECONDS = 23 * 60 * 60  # tokens valid 24h, refresh at 23h


def _require_creds() -> None:
    missing = [
        name for name, val in [
            ("RMS_AGENT_ID", AGENT_ID),
            ("RMS_AGENT_PASSWORD", AGENT_PASSWORD),
            ("RMS_CLIENT_ID", CLIENT_ID),
            ("RMS_CLIENT_PASSWORD", CLIENT_PASSWORD),
        ] if not val
    ]
    if missing:
        raise RuntimeError(
            f"RMS credentials missing: {', '.join(missing)}. "
            "Activate the RMS integration in agent-control and paste the "
            "Agent + Client credentials from your RMS Partner Welcome email."
        )


# ─── token + base-URL cache ────────────────────────────────────────────────

_lock = threading.Lock()
_state: dict[str, Any] = {
    "token": None,
    "token_expiry": 0.0,
    "base_url": None,            # discovered via /clienturl
    "rms_client_id": None,
    "allowed_properties": [],
}


def _module_type_list() -> list[str]:
    return [m.strip() for m in MODULE_TYPE.split(",") if m.strip()] or ["revenueManagement"]


def _discover_base_url() -> str:
    """Resolve the client-pinned base URL.

    Returns BASE_URL_OVERRIDE if set, else hits /clienturl/{rmsClientId}
    on the seed server and uses what comes back. Cached in _state.
    """
    if BASE_URL_OVERRIDE:
        return BASE_URL_OVERRIDE
    if _state["base_url"]:
        return _state["base_url"]
    _require_creds()
    r = requests.get(
        f"{SEED_BASE_URL}/clienturl/{CLIENT_ID}",
        headers={"Accept": "application/json"},
        timeout=15,
    )
    if not r.ok:
        # Couldn't discover — fall back to seed server. Subsequent /authToken
        # may redirect, but RMS's docs say wrong-server traffic just incurs
        # latency, not failure.
        return SEED_BASE_URL
    try:
        url = r.json()
    except ValueError:
        url = r.text.strip().strip('"')
    if not isinstance(url, str) or not url.startswith("http"):
        return SEED_BASE_URL
    url = url.rstrip("/")
    _state["base_url"] = url
    return url


def _fresh_token() -> str:
    """POST /authToken with the configured creds. Caches token + metadata."""
    _require_creds()
    base = _discover_base_url()
    body = {
        "agentId": int(AGENT_ID),
        "agentPassword": AGENT_PASSWORD,
        "clientId": int(CLIENT_ID),
        "clientPassword": CLIENT_PASSWORD,
        "useTrainingDatabase": USE_TRAINING,
        "moduleType": _module_type_list(),
    }
    r = requests.post(
        f"{base}/authToken",
        json=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=20,
    )
    if not r.ok:
        body_preview = (r.text or "")[:400]
        raise RuntimeError(f"RMS /authToken failed ({r.status_code}): {body_preview}")
    data = r.json()
    token = data.get("token")
    if not token:
        raise RuntimeError(f"RMS /authToken returned no token: {data}")
    _state["token"] = token
    _state["token_expiry"] = time.time() + ACCESS_TOKEN_TTL_SECONDS
    _state["rms_client_id"] = data.get("rmsClientId")
    _state["allowed_properties"] = data.get("allowedProperties") or []
    return token


def _token() -> str:
    with _lock:
        now = time.time()
        if _state["token"] and now < _state["token_expiry"]:
            return _state["token"]
        return _fresh_token()


def _invalidate_token() -> None:
    with _lock:
        _state["token"] = None
        _state["token_expiry"] = 0.0


# ─── HTTP helpers ──────────────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    return {
        "authtoken": _token(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, params: dict | None = None, body: Any = None) -> Any:
    """Single-retry wrapper: invalidate cached token on 401 and try again."""
    base = _discover_base_url()
    if not path.startswith("/"):
        path = "/" + path
    url = f"{base}{path}"
    for attempt in (1, 2):
        r = requests.request(
            method, url,
            headers=_headers(),
            params=params,
            json=body,
            timeout=60,
        )
        if r.status_code == 401 and attempt == 1:
            _invalidate_token()
            continue
        break
    if not r.ok:
        body_preview = (r.text or "")[:500]
        raise RuntimeError(f"RMS {method} {path} failed ({r.status_code}): {body_preview}")
    if not r.content:
        return None
    try:
        return r.json()
    except ValueError:
        return r.text


def _get(path: str, params: dict | None = None) -> Any:
    return _request("GET", path, params=params)


def _post(path: str, body: Any = None, params: dict | None = None) -> Any:
    return _request("POST", path, params=params, body=body)


# ─── MCP server ────────────────────────────────────────────────────────────

mcp = FastMCP(
    "rms",
    instructions=(
        "RMS Cloud property management system access (read-only). Single "
        "shared service-account auth — no per-user dance. The integration "
        "automatically caches the 24-hour authToken and discovers the "
        "client-pinned base URL via /clienturl on first use.\n\n"
        "For revenue/occupancy questions, prefer rms_revenue_summary as "
        "the headline tool — it batches the per-reservation dailyRevenue "
        "calls server-side and returns a clean per-day roll-up. Use the "
        "specific /reports endpoints (via rms_report) for occupancy / "
        "performanceII / pace etc."
    ),
)


@mcp.tool()
def rms_who_am_i() -> dict:
    """Diagnostic: confirm the credentials work and report token validity,
    rmsClientId (parent record), and the list of properties accessible.

    Use this first after activation to verify the integration is wired up
    correctly and to discover propertyIds for subsequent calls.
    """
    _require_creds()
    # Force a fresh token call so the response carries up-to-date
    # rmsClientId / allowedProperties — the cached ones may be stale if
    # the operator re-pointed the integration.
    _invalidate_token()
    _token()
    expiry_s = _state["token_expiry"]
    return {
        "linked": True,
        "agent_id": AGENT_ID,
        "client_id": CLIENT_ID,
        "module_type": _module_type_list(),
        "use_training_database": USE_TRAINING,
        "base_url": _discover_base_url(),
        "rms_client_id": _state["rms_client_id"],
        "is_multi_property": (
            _state["rms_client_id"] is not None
            and str(_state["rms_client_id"]) != str(CLIENT_ID)
        ),
        "allowed_properties": _state["allowed_properties"],
        "token_expires_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(expiry_s)
        ) if expiry_s else None,
    }


@mcp.tool()
def rms_properties() -> list[dict]:
    """List properties the credentials have access to.

    GET /properties. Returns the basic property record for each — id,
    name, country, time zone, currency. For multi-property databases this
    is a meaningful list; for a standalone Client like Halcyon House
    (clientId=23267) this typically returns one row.
    """
    data = _get("/properties")
    if not isinstance(data, list):
        return []
    return [
        {
            "id": p.get("id"),
            "name": p.get("name"),
            "country": p.get("country"),
            "currency": p.get("currency"),
            "time_zone": p.get("timeZone") or p.get("timezone"),
            "active": p.get("active") if p.get("active") is not None else p.get("isActive"),
        }
        for p in data if isinstance(p, dict)
    ]


# ─── reservations ──────────────────────────────────────────────────────────

def _summarize_reservation(r: dict) -> dict:
    return {
        "id": r.get("id"),
        "status": r.get("status"),
        "arrival_date": r.get("arrivalDate"),
        "departure_date": r.get("departureDate"),
        "category_id": r.get("categoryId"),
        "area_id": r.get("areaId"),
        "property_id": r.get("propertyId"),
        "rate_id": r.get("rateId"),
        "adults": r.get("adults"),
        "children": r.get("children"),
        "infants": r.get("infants"),
        "guest_id": r.get("guestId") or (r.get("guest") or {}).get("id"),
        "guest_surname": (r.get("guest") or {}).get("surname"),
        "total_amount": r.get("totalAmount"),
        "outstanding": r.get("outstanding"),
    }


@mcp.tool()
def rms_reservations(
    arrive_from: str | None = None,
    arrive_to: str | None = None,
    depart_from: str | None = None,
    depart_to: str | None = None,
    status: str | None = None,
    property_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    """Search reservations by date range / status.

    POST /reservations/search. At least one of the date ranges should be
    set; otherwise you get the entire history (slow + rate-limit risky).

    Args:
        arrive_from: ISO date 'YYYY-MM-DD' lower bound on arrival.
        arrive_to: ISO date 'YYYY-MM-DD' upper bound on arrival.
        depart_from: ISO date 'YYYY-MM-DD' lower bound on departure.
        depart_to: ISO date 'YYYY-MM-DD' upper bound on departure.
        status: One of 'Created', 'Confirmed', 'Arrived', 'Departed',
            'Cancelled', etc. RMS UI 'Made' = API 'Created'.
        property_id: Restrict to one property (multi-property databases).
        limit: Max records to return (default 100, max 500).
    """
    body: dict[str, Any] = {}
    # RMS expects dates as 'YYYY-MM-DD HH:mm:ss' — pad bare dates.
    def _pad(d: str | None, end_of_day: bool = False) -> str | None:
        if not d:
            return None
        if " " in d:
            return d
        return f"{d} {'23:59:59' if end_of_day else '00:00:00'}"
    if arrive_from: body["arriveFrom"] = _pad(arrive_from)
    if arrive_to:   body["arriveTo"]   = _pad(arrive_to, end_of_day=True)
    if depart_from: body["departFrom"] = _pad(depart_from)
    if depart_to:   body["departTo"]   = _pad(depart_to, end_of_day=True)
    if status:      body["statuses"]   = [status]
    if property_id is not None:
        body["propertyIds"] = [int(property_id)]

    params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
    data = _post("/reservations/search", body=body, params=params)
    if not isinstance(data, list):
        return []
    return [_summarize_reservation(r) for r in data if isinstance(r, dict)]


@mcp.tool()
def rms_reservation_revenue(reservation_id: int) -> list[dict]:
    """Daily revenue breakdown for one reservation.

    GET /reservations/{id}/dailyRevenue. Returns a list of per-night
    records with accommodation / F&B / other revenue (and matching
    GST/tax fields). For aggregating across many reservations, use
    rms_revenue_summary instead — this endpoint is rate-limited to
    30 requests/minute.
    """
    if reservation_id is None:
        raise ValueError("reservation_id is required")
    data = _get(f"/reservations/{int(reservation_id)}/dailyRevenue")
    if not isinstance(data, list):
        return []
    return data


# ─── revenue summary (the headline) ────────────────────────────────────────

# /reservations/dailyRevenue/search caps at 50 reservation IDs per call,
# per the spec. We chunk client-side to respect that.
DAILY_REVENUE_BATCH = 50

REVENUE_FIELDS = ("accommodation", "foodAndBeverage", "other")


@mcp.tool()
def rms_revenue_summary(
    start_date: str,
    end_date: str,
    property_id: int | None = None,
    include_uninvoiced: bool = True,
) -> dict:
    """Daily revenue roll-up across a date range — answers "what was last
    week's revenue?".

    Step 1: POST /reservations/search to find reservations that overlap
            [start_date, end_date] (departure >= start, arrival <= end).
    Step 2: POST /reservations/dailyRevenue/search in batches of 50 to
            pull each reservation's per-night revenue.
    Step 3: Aggregate per day, clipped to the requested range, and into
            a grand total.

    Args:
        start_date: ISO 'YYYY-MM-DD' (inclusive).
        end_date: ISO 'YYYY-MM-DD' (inclusive).
        property_id: Optional — restrict to one property (multi-property
            databases). Standalone clients can leave this None.
        include_uninvoiced: If False, skip reservations with 'Cancelled'
            or 'NoShow' status. Default True (matches Tanda's "include
            unapproved with a warning" model — cancelled/noshow shifts
            with revenue=0 don't move the total but show in the source set).

    Returns {start_date, end_date, by_day: [{date, accommodation, food_and_beverage,
    other, total}], totals: {accommodation, food_and_beverage, other, total},
    reservation_count, warnings}. All amounts are tax-inclusive for
    tax-inclusive properties (see RMS's dailyRevenue schema).
    """
    if not (start_date and end_date):
        raise ValueError("start_date and end_date are required (YYYY-MM-DD).")

    # Find candidate reservations: any res that overlaps the window.
    # Overlap = arrival <= end_date AND departure >= start_date.
    res_body: dict[str, Any] = {
        "arriveTo": f"{end_date} 23:59:59",
        "departFrom": f"{start_date} 00:00:00",
    }
    if property_id is not None:
        res_body["propertyIds"] = [int(property_id)]
    if not include_uninvoiced:
        res_body["statuses"] = ["Confirmed", "Arrived", "Departed"]

    reservations: list[dict] = []
    offset = 0
    page_size = 500
    while True:
        page = _post(
            "/reservations/search",
            body=res_body,
            params={"limit": page_size, "offset": offset, "modelType": "lite"},
        )
        if not isinstance(page, list) or not page:
            break
        reservations.extend(page)
        if len(page) < page_size:
            break
        offset += page_size

    res_ids = [r["id"] for r in reservations if isinstance(r, dict) and r.get("id")]

    # Aggregator setup.
    by_day: dict[str, dict[str, float]] = {}
    totals = {f: 0.0 for f in REVENUE_FIELDS}
    totals["total"] = 0.0

    def _bucket(d: str) -> dict[str, float]:
        return by_day.setdefault(d, {f: 0.0 for f in REVENUE_FIELDS} | {"total": 0.0})

    # Batch through dailyRevenue/search.
    for i in range(0, len(res_ids), DAILY_REVENUE_BATCH):
        chunk = res_ids[i:i + DAILY_REVENUE_BATCH]
        rows = _post("/reservations/dailyRevenue/search", body={"ids": chunk})
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            entries = row.get("dailyRevenue") or []
            if not isinstance(entries, list):
                continue
            for e in entries:
                if not isinstance(e, dict):
                    continue
                # theDate may come back as 'YYYY-MM-DD' or
                # 'YYYY-MM-DD HH:mm:ss' — normalize to the date part.
                d = (e.get("theDate") or "")[:10]
                if not d or d < start_date or d > end_date:
                    continue
                bucket = _bucket(d)
                row_total = 0.0
                for f in REVENUE_FIELDS:
                    v = e.get(f)
                    if isinstance(v, (int, float)):
                        bucket[f] += float(v)
                        totals[f] += float(v)
                        row_total += float(v)
                bucket["total"] += row_total
                totals["total"] += row_total

    # Sorted day-by-day output, dates filled even if zero so the bot can
    # show a clean week.
    from datetime import date, timedelta
    s = date.fromisoformat(start_date)
    e = date.fromisoformat(end_date)
    rows_out: list[dict] = []
    d = s
    while d <= e:
        ds = d.isoformat()
        b = by_day.get(ds, {f: 0.0 for f in REVENUE_FIELDS} | {"total": 0.0})
        rows_out.append({
            "date": ds,
            "accommodation": round(b["accommodation"], 2),
            "food_and_beverage": round(b["foodAndBeverage"], 2),
            "other": round(b["other"], 2),
            "total": round(b["total"], 2),
        })
        d += timedelta(days=1)

    return {
        "start_date": start_date,
        "end_date": end_date,
        "by_day": rows_out,
        "totals": {
            "accommodation": round(totals["accommodation"], 2),
            "food_and_beverage": round(totals["foodAndBeverage"], 2),
            "other": round(totals["other"], 2),
            "total": round(totals["total"], 2),
        },
        "reservation_count": len(res_ids),
        "warnings": [],
    }


# ─── reports ───────────────────────────────────────────────────────────────

KNOWN_REPORTS = {
    "areaIncomeSummary", "auditTrail", "cash", "charge", "debtorsLedger",
    "expensesAreaSummary", "flash", "historyForecast", "npsResults",
    "nightAudit", "occupancy", "occupancyByArea", "occupancyRevenueComparison",
    "pace", "pathfinder", "performanceII", "revenueAndExpense",
}


@mcp.tool()
def rms_report(report_type: str, params_json: str | None = None) -> Any:
    """Run an RMS built-in report via POST /reports/{report_type}.

    Args:
        report_type: One of: areaIncomeSummary, auditTrail, cash, charge,
            debtorsLedger, expensesAreaSummary, flash, historyForecast,
            npsResults, nightAudit, occupancy, occupancyByArea,
            occupancyRevenueComparison, pace, pathfinder, performanceII,
            revenueAndExpense.
        params_json: JSON object matching the specific report's request
            body. Most reports want a propertyId and dateFrom/dateTo at
            minimum; consult the RMS REST API spec for details.

    Rate-limited at 60 requests/minute across all /reports* endpoints.
    """
    if not report_type:
        raise ValueError("report_type is required")
    if report_type not in KNOWN_REPORTS:
        raise ValueError(
            f"report_type {report_type!r} is not in the known list. "
            f"Known: {sorted(KNOWN_REPORTS)}"
        )
    body: Any = None
    if params_json:
        try:
            body = json.loads(params_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"params_json is not valid JSON: {e}") from e
    return _post(f"/reports/{report_type}", body=body)


# ─── escape hatches ────────────────────────────────────────────────────────

@mcp.tool()
def rms_raw_get(path: str, params_json: str | None = None) -> Any:
    """GET an arbitrary RMS REST API path. Path is relative to the
    discovered/configured base URL. Use only for endpoints not wrapped by
    the typed tools above.
    """
    if not path:
        raise ValueError("path is required")
    params = None
    if params_json:
        try:
            params = json.loads(params_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"params_json is not valid JSON: {e}") from e
        if not isinstance(params, dict):
            raise ValueError("params_json must encode a JSON object")
    return _get(path, params=params)


@mcp.tool()
def rms_raw_post(path: str, body_json: str | None = None) -> Any:
    """POST an arbitrary RMS REST API path with a JSON body. Read-only by
    intent — only use against documented search/report POSTs.
    """
    if not path:
        raise ValueError("path is required")
    body = None
    if body_json:
        try:
            body = json.loads(body_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"body_json is not valid JSON: {e}") from e
    return _post(path, body=body)


# ─── entry ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
