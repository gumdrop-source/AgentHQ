"""MYOB AccountRight Live MCP server — read-only access for AgentHQ.

Reads the chart of accounts, P&L reports, and employee payroll details
(including leave entitlements) from a single company file.

Auth: OAuth2 refresh-token grant against secure.myob.com. The MYOB token
service rotates the refresh_token on every refresh response — the rotated
value is persisted to $HOME/myob_refresh_token (mode 0600) so subsequent
restarts pick it up automatically. The original encrypted seed at
/etc/agents/credentials/myob_refresh_token.cred only matters for first
onboarding; the writable cache takes over after the first refresh.

Credentials are injected by /opt/agents/bin/agent-mcp-launcher from the
systemd-creds vault, exported under the env names declared in tool.json.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP

# ─── credentials ────────────────────────────────────────────────────────────

# All required env vars are surfaced lazily — the server still imports
# cleanly when creds are missing so FastMCP can register tools and surface
# a clear error on first call rather than crashing the stdio handshake.
CLIENT_ID = os.environ.get("MYOB_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("MYOB_CLIENT_SECRET", "")
SEED_REFRESH_TOKEN = os.environ.get("MYOB_REFRESH_TOKEN", "")
BUSINESS_ID = os.environ.get("MYOB_BUSINESS_ID", "")

HOME = Path(os.environ.get("HOME", "/tmp"))
REFRESH_TOKEN_FILE = HOME / "myob_refresh_token"

OAUTH_URL = "https://secure.myob.com/oauth2/v1/authorize"
COMPANY_FILE_BASE = f"https://api.myob.com/accountright/{BUSINESS_ID}" if BUSINESS_ID else ""

# MYOB access tokens are valid for 20 min. Refresh at 18 to leave a safety
# margin for in-flight requests.
ACCESS_TOKEN_TTL_SECONDS = 18 * 60


def _require_creds() -> None:
    missing = [
        name
        for name, val in [
            ("MYOB_CLIENT_ID", CLIENT_ID),
            ("MYOB_CLIENT_SECRET", CLIENT_SECRET),
            ("MYOB_BUSINESS_ID", BUSINESS_ID),
        ]
        if not val
    ]
    if missing:
        raise RuntimeError(
            f"MYOB credentials missing: {', '.join(missing)}. "
            "The operator must activate the myob integration in agent-control "
            "(which provisions the encrypted credentials and reloads the agent service)."
        )


# ─── refresh-token persistence ──────────────────────────────────────────────

def _load_refresh_token() -> str:
    """Return the most recent refresh_token.

    Priority:
      1. $HOME/myob_refresh_token  — written by this process after the
         most recent successful refresh (rotated value, freshest).
      2. $MYOB_REFRESH_TOKEN       — the encrypted seed staged by systemd
         on first onboarding.

    The seed is only authoritative the very first time the tool runs on
    a new agent; once we successfully refresh once, the file wins.
    """
    if REFRESH_TOKEN_FILE.exists():
        try:
            value = REFRESH_TOKEN_FILE.read_text().strip()
            if value:
                return value
        except OSError:
            pass
    return SEED_REFRESH_TOKEN


def _save_refresh_token(value: str) -> None:
    """Persist the rotated refresh_token to the per-agent writable cache.

    Writes through a tempfile rename so a concurrent reader never sees a
    half-written file. Mode 0600 — only the -mcp user (us) can read it.
    """
    if not value:
        return
    tmp = REFRESH_TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(value)
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(REFRESH_TOKEN_FILE)


# ─── access-token cache ─────────────────────────────────────────────────────

# Single in-process cache + lock around the refresh exchange. Multiple
# tool calls in the same MCP session share the cache; if two land in the
# same window only one round-trip to MYOB happens.
_token_lock = threading.Lock()
# Cached access token + expiry (Unix epoch seconds). Underscored-with-suffix
# to avoid colliding with the _access_token() accessor below — Python has no
# warning when a name on a `def` line shadows a module-level variable, and
# `global _access_token` inside the function would silently reassign the
# function reference itself, breaking the next call with "'str' object is
# not callable".
_cached_access_token: str | None = None
_cached_access_token_expires_at: float = 0.0


def _refresh_access_token() -> str:
    """Exchange the current refresh_token for a fresh access_token.

    MYOB rotates the refresh_token on every response — we MUST persist
    the new value or we'll be locked out on the next restart.
    """
    _require_creds()
    rt = _load_refresh_token()
    if not rt:
        raise RuntimeError(
            "No MYOB refresh token available. Provision one via "
            "`sudo agenthq-cred set myob_refresh_token` (see tools/myob/setup.md)."
        )
    r = requests.post(
        OAUTH_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": rt,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(
            f"MYOB token refresh failed ({r.status_code}): {r.text[:300]}. "
            "If this says invalid_grant, the refresh token has been revoked "
            "(another client used it, or the user changed password) — re-run "
            "the OAuth re-consent recipe in tools/myob/setup.md."
        )
    payload = r.json()
    access = payload["access_token"]
    new_rt = payload.get("refresh_token")
    if new_rt and new_rt != rt:
        _save_refresh_token(new_rt)
    return access


def _access_token() -> str:
    global _cached_access_token, _cached_access_token_expires_at
    with _token_lock:
        now = time.time()
        if _cached_access_token and now < _cached_access_token_expires_at:
            return _cached_access_token
        token = _refresh_access_token()
        _cached_access_token = token
        _cached_access_token_expires_at = now + ACCESS_TOKEN_TTL_SECONDS
        return token


# ─── HTTP helpers ───────────────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_access_token()}",
        "x-myobapi-key": CLIENT_ID,
        "x-myobapi-version": "v2",
        "Accept": "application/json",
    }


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET against the company-file root. `path` is appended to
    /accountright/<business_id>, so callers pass e.g. "/Contact/Employee"."""
    _require_creds()
    if not path.startswith("/"):
        path = "/" + path
    url = f"{COMPANY_FILE_BASE}{path}"
    r = requests.get(url, headers=_headers(), params=params, timeout=30)
    if r.status_code == 401:
        # Stale access token (clock skew, MYOB invalidated early). Force a
        # refresh and retry once before failing the call.
        global _cached_access_token_expires_at
        _cached_access_token_expires_at = 0.0
        r = requests.get(url, headers=_headers(), params=params, timeout=30)
    if not r.ok:
        # Surface MYOB's error envelope into the message so the agent (and
        # future-Claude debugging this) sees what actually failed instead of
        # a bare "400 Client Error". MYOB returns JSON like
        # {"Errors":[{"Message":"...","AdditionalDetails":"..."}]}.
        body = r.text[:500] if r.text else ""
        raise RuntimeError(f"MYOB GET {path} failed ({r.status_code}): {body}")
    if not r.content:
        return None
    return r.json()


# ─── MCP server ─────────────────────────────────────────────────────────────

mcp = FastMCP(
    "myob",
    instructions=(
        "MYOB AccountRight Live access (read-only). Tools cover the chart of "
        "accounts, profit & loss reports, and employee payroll details "
        "including leave entitlements. All data comes from a single company "
        "file configured by the operator. Do NOT attempt write operations — "
        "this app registration is read-only by design."
    ),
)


# ─── company file metadata ──────────────────────────────────────────────────

@mcp.tool()
def myob_company_file_info() -> dict:
    """Return company file metadata (name, AccountRight product/version,
    last sync time, country, currency). Useful as a connectivity sanity
    check before running larger queries."""
    return _get("/")


# ─── general ledger ─────────────────────────────────────────────────────────

# Restrict to MYOB's documented Classification values so a typo in the
# argument fails clearly here rather than as an opaque MYOB filter error.
ACCOUNT_CLASSIFICATIONS = {
    "Asset", "Liability", "Equity", "Income", "Expense",
    "CostOfSales", "OtherIncome", "OtherExpense",
}


@mcp.tool()
def myob_accounts(
    class_filter: str | None = None,
    account_number_prefix: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """List the chart of accounts.

    Args:
        class_filter: Restrict to one classification — Asset, Liability,
            Equity, Income, Expense, CostOfSales, OtherIncome, OtherExpense.
        account_number_prefix: Restrict to accounts whose DisplayID starts
            with this string (e.g. "4-" for income accounts in many setups).
        limit: Max accounts to return (default 500, MYOB's hard cap is 1000).
    """
    params: dict[str, Any] = {"$top": min(max(limit, 1), 1000)}
    filters: list[str] = []
    if class_filter:
        if class_filter not in ACCOUNT_CLASSIFICATIONS:
            raise ValueError(
                f"class_filter must be one of {sorted(ACCOUNT_CLASSIFICATIONS)}, "
                f"got {class_filter!r}"
            )
        filters.append(f"Classification eq '{class_filter}'")
    if account_number_prefix:
        # MYOB's $filter doesn't support startswith on DisplayID reliably —
        # do prefix matching client-side so we don't trip InefficientFilter.
        pass
    if filters:
        params["$filter"] = " and ".join(filters)

    data = _get("/GeneralLedger/Account", params=params)
    items = data.get("Items", []) if isinstance(data, dict) else []
    if account_number_prefix:
        items = [a for a in items if (a.get("DisplayID") or "").startswith(account_number_prefix)]
    return [
        {
            "uid": a.get("UID"),
            "display_id": a.get("DisplayID"),
            "name": a.get("Name"),
            "classification": a.get("Classification"),
            "type": a.get("Type"),
            "level": a.get("Level"),
            "is_header": a.get("IsHeader"),
            "is_active": a.get("IsActive"),
            "current_balance": a.get("CurrentBalance"),
            "tax_code": (a.get("TaxCode") or {}).get("Code"),
        }
        for a in items
    ]


# ─── reports ────────────────────────────────────────────────────────────────

REPORTING_BASES = {"Accrual", "Cash"}


def _pl_call(start_date: str, end_date: str, basis: str) -> dict:
    if basis not in REPORTING_BASES:
        raise ValueError(f"basis must be 'Accrual' or 'Cash', got {basis!r}")
    # MYOB requires YearEndAdjust on the P&L endpoint as of late 2025.
    # `false` matches what the AccountRight UI uses by default — only flip
    # to `true` if you want the report to include year-end posting
    # adjustments (typically only relevant for the closing month of FY).
    return _get(
        "/Report/ProfitAndLossSummary",
        params={
            "startDate": start_date,
            "endDate": end_date,
            "reportingBasis": basis,
            "yearEndAdjust": "false",
        },
    )


@mcp.tool()
def myob_pl_summary(
    start_date: str,
    end_date: str,
    basis: str = "Accrual",
) -> dict:
    """Profit & Loss summary report for one date range.

    Args:
        start_date: ISO date 'YYYY-MM-DD' (inclusive).
        end_date: ISO date 'YYYY-MM-DD' (inclusive).
        basis: 'Accrual' (default) or 'Cash'.

    Returns the full MYOB report payload — header info plus an array of
    line items grouped by classification (Income, Expense, etc.).
    """
    return _pl_call(start_date, end_date, basis)


@mcp.tool()
def myob_pl_compare(
    periods_json: str,
    basis: str = "Accrual",
) -> list[dict]:
    """Run P&L summary across multiple periods and return them side-by-side.

    Args:
        periods_json: JSON array of {label, start_date, end_date} objects.
            Example:
              [
                {"label": "FY24", "start_date": "2023-07-01", "end_date": "2024-06-30"},
                {"label": "FY25 to date", "start_date": "2024-07-01", "end_date": "2025-04-30"}
              ]
        basis: 'Accrual' (default) or 'Cash' — applied to every period.

    Each output element is {label, start_date, end_date, report}.
    """
    try:
        periods = json.loads(periods_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"periods_json is not valid JSON: {e}") from e
    if not isinstance(periods, list) or not periods:
        raise ValueError("periods_json must be a non-empty JSON array")

    out: list[dict] = []
    for p in periods:
        start = p.get("start_date")
        end = p.get("end_date")
        label = p.get("label") or f"{start}..{end}"
        if not (start and end):
            raise ValueError(f"period missing start_date/end_date: {p!r}")
        out.append(
            {
                "label": label,
                "start_date": start,
                "end_date": end,
                "report": _pl_call(start, end, basis),
            }
        )
    return out


# ─── employees / payroll ────────────────────────────────────────────────────

# MYOB OData rejects identifiers containing single quotes inside string
# literals unless we double them. Names with apostrophes (e.g. O'Brien)
# would otherwise produce a 400 with a confusing parse-error message.
def _odata_str(value: str) -> str:
    return value.replace("'", "''")


def _summarize_employee(emp: dict) -> dict:
    # MYOB returns a deeply nested record; flatten to the bits the LLM needs
    # to act, with a pointer to the full record's UID for drill-down.
    payroll = emp.get("EmployeePayrollDetails") or {}
    return {
        "uid": emp.get("UID"),
        "display_id": emp.get("DisplayID"),
        "first_name": emp.get("FirstName"),
        "last_name": emp.get("LastName"),
        "is_individual": emp.get("IsIndividual"),
        "is_active": emp.get("IsActive"),
        "email": (emp.get("Addresses") or [{}])[0].get("Email") if emp.get("Addresses") else None,
        "payroll_details_uid": payroll.get("UID"),
    }


@mcp.tool()
def myob_employee_lookup(
    first_name: str | None = None,
    last_name: str | None = None,
) -> list[dict]:
    """Find employees whose first or last name matches (exact, case-sensitive).

    Args:
        first_name: Match on FirstName. Combined with last_name via OR.
        last_name: Match on LastName.

    At least one of first_name / last_name is required. MYOB's $filter
    only supports `eq` / `ne` (not `contains`) — pass exact strings.
    Returns a list of compact employee summaries each with a UID for
    further drill-down via myob_employee_payroll_details.
    """
    if not (first_name or last_name):
        raise ValueError("Provide at least one of first_name or last_name.")

    clauses: list[str] = []
    if first_name:
        clauses.append(f"FirstName eq '{_odata_str(first_name)}'")
    if last_name:
        clauses.append(f"LastName eq '{_odata_str(last_name)}'")
    params = {"$filter": " or ".join(clauses)}
    data = _get("/Contact/Employee", params=params)
    items = data.get("Items", []) if isinstance(data, dict) else []
    return [_summarize_employee(e) for e in items]


@mcp.tool()
def myob_employee_payroll_details(uid: str) -> dict:
    """Full payroll record for one employee by UID.

    Returns AnnualSalary, HourlyRate, PayFrequency, HoursInWeeklyPayPeriod,
    StartDate, WageCategories, Superannuation, Tax, and the full
    Entitlements array (leave balances).

    Get the UID from myob_employee_lookup. The UID for the payroll record
    is the same as the EmployeePayrollDetails.UID value returned there.
    """
    if not uid:
        raise ValueError("uid is required")
    return _get(f"/Contact/EmployeePayrollDetails/{uid}")


@mcp.tool()
def myob_employee_standard_pay(uid: str) -> dict:
    """Standard (recurring) pay configuration for one employee by UID.

    This is the template MYOB applies each pay run before adjustments —
    useful for confirming someone's expected fortnightly pay or which
    wage/entitlement/super categories accrue automatically.
    """
    if not uid:
        raise ValueError("uid is required")
    return _get(f"/Contact/EmployeeStandardPay/{uid}")


# Hours per workday for converting leave hours → days. MYOB doesn't expose
# this on the entitlement record so we infer from Wage.HoursInWeeklyPayPeriod
# combined with PayFrequency. The field name is misleading: for Fortnightly
# employees it's actually hours over a fortnight (80 = 8h × 10 working days),
# not over a single week. Always divide by working-days-in-period.
PERIOD_WORKING_DAYS = {
    "Weekly": 5,
    "Fortnightly": 10,
    "Monthly": 22,           # standard payroll convention: 22 working days/mo
    "Bimonthly": 11,         # twice per month — half a month of working days
    "Quarterly": 65,         # ~22 × 3
}


def _hours_per_day(payroll: dict) -> float:
    wage = payroll.get("Wage") or {}
    period_hours = wage.get("HoursInWeeklyPayPeriod")
    pay_freq = wage.get("PayFrequency") or "Weekly"
    days = PERIOD_WORKING_DAYS.get(pay_freq, 5)
    if isinstance(period_hours, (int, float)) and period_hours > 0 and days > 0:
        return float(period_hours) / float(days)
    # Fall back to Australia's notional 38h week / 5 days = 7.6h/day.
    return 7.6


@mcp.tool()
def myob_employee_leave_balance(name: str) -> dict:
    """Convenience: look up an employee by name, then return only their
    assigned leave entitlements (sick, holiday, long-service, etc.) with
    hours and an estimated days-equivalent.

    Args:
        name: First or last name. Tries last name first, then first name.

    Returns {employee, hours_per_day, entitlements: [{name, type, hours, days, ...}]}
    """
    if not name or not name.strip():
        raise ValueError("name is required")
    needle = name.strip()

    # Try fast OData exact-match queries first (cheap, indexed). Last name
    # is more selective in most orgs, then first name.
    matches = myob_employee_lookup(last_name=needle)
    if not matches:
        matches = myob_employee_lookup(first_name=needle)

    # Fallback: MYOB OData has no contains() / startswith() that we can rely
    # on, so a name like "Saraiva" never matches LastName="Dos Santos
    # Saraiva". Fetch the full active-employee list (small enough — most
    # AccountRight files have <500 employees) and substring-match
    # case-insensitively against First/Last/DisplayID.
    if not matches:
        try:
            data = _get("/Contact/Employee", params={"$top": 1000})
            all_emps = data.get("Items", []) if isinstance(data, dict) else []
        except Exception:
            all_emps = []
        ndl = needle.lower()
        fuzzy = [
            e for e in all_emps
            if ndl in (e.get("FirstName") or "").lower()
            or ndl in (e.get("LastName") or "").lower()
            or ndl in (e.get("DisplayID") or "").lower()
        ]
        matches = [_summarize_employee(e) for e in fuzzy]

    if not matches:
        return {
            "employee": None,
            "matches": [],
            "message": f"No employee found matching {name!r}.",
        }
    if len(matches) > 1:
        return {
            "employee": None,
            "matches": matches,
            "message": (
                f"{len(matches)} employees match {name!r}. Resolve by calling "
                "myob_employee_payroll_details with the UID you want."
            ),
        }

    emp = matches[0]
    payroll_uid = emp.get("payroll_details_uid") or emp.get("uid")
    payroll = _get(f"/Contact/EmployeePayrollDetails/{payroll_uid}")

    hpd = _hours_per_day(payroll)
    raw_entitlements = payroll.get("Entitlements") or []
    assigned = []
    for ent in raw_entitlements:
        if not ent.get("IsAssigned"):
            continue
        carry = ent.get("CarryOver") or 0.0
        ytd = ent.get("YearToDate") or 0.0
        total = ent.get("Total")
        if total is None:
            total = (carry or 0.0) + (ytd or 0.0)
        # MYOB nests the category under "EntitlementCategory" on entitlement
        # records (not "PayrollCategory" — that's the wage-record key).
        cat = ent.get("EntitlementCategory") or ent.get("PayrollCategory") or {}
        assigned.append(
            {
                "name": cat.get("Name"),
                "uid": cat.get("UID"),
                "carry_over_hours": carry,
                "year_to_date_hours": ytd,
                "total_hours": total,
                "days_equivalent": round(total / hpd, 2) if hpd else None,
            }
        )

    return {
        "employee": {
            "uid": emp.get("uid"),
            "first_name": emp.get("first_name"),
            "last_name": emp.get("last_name"),
            "display_id": emp.get("display_id"),
        },
        "hours_per_day": round(hpd, 2),
        "entitlements": assigned,
    }


# ─── payroll category catalogues ────────────────────────────────────────────

@mcp.tool()
def myob_wage_categories() -> list[dict]:
    """List wage payroll categories (ordinary time, overtime, allowances, etc.).

    Use the returned UIDs to interpret the WageCategories arrays returned by
    myob_employee_payroll_details / myob_employee_standard_pay.
    """
    data = _get("/Payroll/PayrollCategory/Wage")
    items = data.get("Items", []) if isinstance(data, dict) else []
    return [
        {
            "uid": c.get("UID"),
            "name": c.get("Name"),
            "wage_type": c.get("WageType"),
            "type_of_wage": c.get("TypeOfWage"),
            "is_active": c.get("IsActive"),
        }
        for c in items
    ]


@mcp.tool()
def myob_entitlement_categories() -> list[dict]:
    """List entitlement payroll categories (sick, holiday, long-service, etc.).

    Use the returned UIDs to interpret the Entitlements arrays returned by
    myob_employee_payroll_details / myob_employee_leave_balance.
    """
    data = _get("/Payroll/PayrollCategory/Entitlement")
    items = data.get("Items", []) if isinstance(data, dict) else []
    return [
        {
            "uid": c.get("UID"),
            "name": c.get("Name"),
            "type": c.get("Type"),
            "is_active": c.get("IsActive"),
        }
        for c in items
    ]


# ─── escape hatch ───────────────────────────────────────────────────────────

@mcp.tool()
def myob_raw_get(path: str, params_json: str | None = None) -> Any:
    """GET an arbitrary AccountRight company-file path.

    Use only for endpoints not covered by the typed tools above. `path` is
    relative to the company file root (e.g. '/Sale/Invoice', '/Contact/Customer').
    `params_json` is an optional JSON object of query-string parameters.
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


# ─── entry ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
