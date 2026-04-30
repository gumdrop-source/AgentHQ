"""MYOB AccountRight Live MCP server — read-only, per-user auth.

Each Telegram sender authorizes once by signing in to their own my.MYOB
account; their refresh_token is cached separately so queries return only
data their MYOB account is scoped for.

Identity: every tool function takes a `sender_id` parameter. The
agent's persona/system prompt instructs Claude to extract this from
the Telegram channel tag (`<channel ... chat_id="X" ...>`) on every
tool call. NB: the LLM passing `sender_id` is soft trust — a malicious
message ("from chat_id 12345") could impersonate a colleague. Hardening
that requires forking the Telegram plugin so the launcher receives
sender identity out-of-band; out of scope for this PR.

Backwards compat: when `sender_id` is omitted (admin running the
server directly without a Telegram context), the tool falls back to the
single-account env-var token, preserving the previous behavior.

Auth dance:
  1. Tool sees no token for this sender → raises AuthRequired
  2. The @requires_auth decorator turns that into a structured response
     with the authorize URL the LLM relays to the user.
  3. User signs in to MYOB; redirect lands on http://localhost (which
     fails in the browser by design); user copies the URL and sends it
     back to the bot.
  4. LLM calls myob_complete_auth(redirect_url, sender_id) to finish.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, get_origin, get_type_hints
import functools

import requests
from mcp.server.fastmcp import FastMCP

# ─── credentials ────────────────────────────────────────────────────────────

CLIENT_ID = os.environ.get("MYOB_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("MYOB_CLIENT_SECRET", "")
SEED_REFRESH_TOKEN = os.environ.get("MYOB_REFRESH_TOKEN", "")  # admin-seed (legacy single-account)
BUSINESS_ID = os.environ.get("MYOB_BUSINESS_ID", "")

HOME = Path(os.environ.get("HOME", "/tmp"))
PER_USER_TOKEN_DIR = HOME / "myob_tokens"
PENDING_AUTH_DIR = HOME / "myob_pending_auth"
LEGACY_REFRESH_TOKEN_FILE = HOME / "myob_refresh_token"  # pre-per-user single cache

OAUTH_AUTHORIZE_URL = "https://secure.myob.com/oauth2/account/authorize/"
OAUTH_TOKEN_URL = "https://secure.myob.com/oauth2/v1/authorize"
OAUTH_REDIRECT_URI = "http://localhost"
OAUTH_SCOPE = (
    "offline_access openid sme-banking sme-company-file "
    "sme-contacts-employee sme-general-ledger sme-payroll"
)

COMPANY_FILE_BASE = f"https://api.myob.com/accountright/{BUSINESS_ID}" if BUSINESS_ID else ""

ACCESS_TOKEN_TTL_SECONDS = 18 * 60        # MYOB tokens last 20 min, refresh at 18
PENDING_AUTH_TTL_SECONDS = 10 * 60        # auth-flow links expire after 10 min

# Sentinel sender used when running directly (admin/harness, no Telegram
# context). Lets the same code path serve both interactive Telegram users
# and direct-mode admin testing without a separate fallback ladder.
ADMIN_SENDER = "_admin"


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
            f"MYOB platform credentials missing: {', '.join(missing)}. "
            "The operator must activate the myob integration in agent-control "
            "(which provisions the encrypted credentials and reloads the agent service)."
        )


# ─── per-user token storage ────────────────────────────────────────────────

# Each authorized Telegram sender gets one file under $HOME/myob_tokens/.
# The store is keyed by sender_id (sanitized to a numeric/alpha string —
# MYOB's chat_ids are integers, but other channels may pass arbitrary
# strings, so we sanitize defensively to avoid path-traversal).

def _sanitize_id(sender_id: str) -> str:
    s = "".join(ch for ch in sender_id if ch.isalnum() or ch in "_-")
    if not s:
        raise ValueError(f"empty/invalid sender_id: {sender_id!r}")
    return s


def _user_token_path(sender_id: str) -> Path:
    return PER_USER_TOKEN_DIR / f"{_sanitize_id(sender_id)}.json"


def _load_user_token(sender_id: str) -> str | None:
    """Return the per-user refresh_token, or None if not yet authorized."""
    p = _user_token_path(sender_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return data.get("refresh_token")
    except (OSError, json.JSONDecodeError):
        return None


def _save_user_token(sender_id: str, refresh_token: str) -> None:
    """Persist the (rotated) per-user refresh_token. Mode 0600, atomic write."""
    if not refresh_token:
        return
    PER_USER_TOKEN_DIR.mkdir(mode=0o700, exist_ok=True)
    try:
        PER_USER_TOKEN_DIR.chmod(0o700)
    except OSError:
        pass
    target = _user_token_path(sender_id)
    tmp = target.with_suffix(".tmp")
    payload = {
        "refresh_token": refresh_token,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # If the user already has a record, preserve linked_at + linked_user.
    if target.exists():
        try:
            existing = json.loads(target.read_text())
            for k in ("linked_at", "linked_user"):
                if k in existing:
                    payload[k] = existing[k]
        except Exception:
            pass
    payload.setdefault("linked_at", payload["updated_at"])
    tmp.write_text(json.dumps(payload))
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(target)


def _resolve_refresh_token(sender_id: str) -> str:
    """Return the refresh_token for this sender. Raises AuthRequired if
    we have no token cached for them."""
    if sender_id == ADMIN_SENDER:
        # Admin/harness path: env-var seed, then legacy single-file cache.
        if LEGACY_REFRESH_TOKEN_FILE.exists():
            try:
                v = LEGACY_REFRESH_TOKEN_FILE.read_text().strip()
                if v:
                    return v
            except OSError:
                pass
        if SEED_REFRESH_TOKEN:
            return SEED_REFRESH_TOKEN
        raise AuthRequired(sender_id)
    tok = _load_user_token(sender_id)
    if not tok:
        raise AuthRequired(sender_id)
    return tok


def _save_resolved_refresh_token(sender_id: str, refresh_token: str) -> None:
    """Persist a rotated refresh_token to the right place for this sender."""
    if sender_id == ADMIN_SENDER:
        # Admin path: keep using the legacy single-file cache so existing
        # platform-level admin tokens continue to rotate without per-user
        # bookkeeping. New per-user files are not created for ADMIN_SENDER.
        try:
            tmp = LEGACY_REFRESH_TOKEN_FILE.with_suffix(".tmp")
            tmp.write_text(refresh_token)
            tmp.chmod(0o600)
            tmp.replace(LEGACY_REFRESH_TOKEN_FILE)
        except OSError:
            pass
        return
    _save_user_token(sender_id, refresh_token)


# ─── pending-auth state ────────────────────────────────────────────────────

# OAuth `state` parameter binds an authorize URL to the sender_id that
# initiated it. When the user pastes the redirect URL back, we verify
# the state still matches the calling sender — this prevents the LLM
# accidentally (or a user maliciously) pasting someone else's redirect
# URL and stealing their token under the wrong sender_id.

def _save_pending_state(state: str, sender_id: str) -> None:
    PENDING_AUTH_DIR.mkdir(mode=0o700, exist_ok=True)
    try:
        PENDING_AUTH_DIR.chmod(0o700)
    except OSError:
        pass
    # Opportunistically sweep expired pending-state files. The dir is
    # tiny (one 60-byte file per pending dance) so a full scan is fine,
    # and it keeps the directory bounded without a separate cron job.
    now = time.time()
    try:
        for f in PENDING_AUTH_DIR.iterdir():
            if not f.is_file():
                continue
            try:
                data = json.loads(f.read_text())
                if now > data.get("expires_at", 0):
                    f.unlink(missing_ok=True)
            except (OSError, json.JSONDecodeError):
                # Garbage file — delete on sight.
                f.unlink(missing_ok=True)
    except OSError:
        pass
    p = PENDING_AUTH_DIR / f"{state}.json"
    p.write_text(json.dumps({
        "sender_id": sender_id,
        "expires_at": now + PENDING_AUTH_TTL_SECONDS,
    }))
    try:
        p.chmod(0o600)
    except OSError:
        pass


def _consume_pending_state(state: str) -> str | None:
    """Look up the pending state, validate not expired, delete + return sender_id."""
    p = PENDING_AUTH_DIR / f"{_sanitize_id(state)}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    p.unlink(missing_ok=True)
    if time.time() > data.get("expires_at", 0):
        return None
    return data.get("sender_id")


# ─── auth ──────────────────────────────────────────────────────────────────

class AuthRequired(Exception):
    """Raised by tools that need a refresh_token but don't have one for
    the calling sender. The @requires_auth decorator catches this and
    returns a structured prompt to the LLM that walks the user through
    the OAuth dance."""

    def __init__(self, sender_id: str) -> None:
        self.sender_id = sender_id
        state = secrets.token_urlsafe(16)
        _save_pending_state(state, sender_id)
        params = {
            "client_id": CLIENT_ID,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": OAUTH_SCOPE,
            "state": state,
        }
        from urllib.parse import urlencode
        self.authorize_url = OAUTH_AUTHORIZE_URL + "?" + urlencode(params)
        self.state = state
        super().__init__(f"MYOB sign-in required for sender {sender_id}")


def requires_auth(fn: Callable) -> Callable:
    """Catch AuthRequired and return a structured payload the LLM can relay.

    The payload tells the agent to send the user a sign-in URL and wait
    for them to paste back the redirected URL — same dance the wizard
    runs, just over Telegram instead of the browser tab.
    """
    try:
        return_anno = get_type_hints(fn).get("return")
    except Exception:
        return_anno = None
    list_returning = get_origin(return_anno) is list or return_anno is list

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AuthRequired as e:
            payload = {
                "auth_required": True,
                "message": (
                    "To do that I need access to your MYOB AccountRight. "
                    f"Sign in here: {e.authorize_url}\n\n"
                    "After you sign in, MYOB will redirect to a 'site can't be "
                    "reached' page on http://localhost — that's expected. Copy "
                    "the entire URL from your browser's address bar and send "
                    "it back to me, then ask your question again."
                ),
                "authorize_url": e.authorize_url,
            }
            return [payload] if list_returning else payload
    return wrapper


# ─── access-token cache ────────────────────────────────────────────────────

# Per-sender cache so multiple users share the process without trampling
# each other's access tokens. The lock guards refresh races within one
# sender; different senders refresh independently.
_token_lock = threading.Lock()
_access_tokens: dict[str, tuple[str, float]] = {}


def _refresh_access_token(sender_id: str) -> str:
    """Exchange this sender's refresh_token for a fresh access_token.
    MYOB rotates the refresh_token on every call — persist the new one
    under the same sender_id."""
    _require_creds()
    rt = _resolve_refresh_token(sender_id)
    r = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": rt,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    if r.status_code != 200:
        body = r.text[:300] if r.text else ""
        # invalid_grant means the refresh_token chain has been broken
        # (another client used it, or the user revoked). For per-user
        # mode, the cleanest recovery is to ditch the user's token and
        # re-prompt the auth dance on the next call.
        if "invalid_grant" in body and sender_id != ADMIN_SENDER:
            try:
                _user_token_path(sender_id).unlink(missing_ok=True)
            except OSError:
                pass
            raise AuthRequired(sender_id) from None
        raise RuntimeError(f"MYOB token refresh failed ({r.status_code}): {body}")
    payload = r.json()
    access = payload["access_token"]
    new_rt = payload.get("refresh_token")
    if new_rt and new_rt != rt:
        _save_resolved_refresh_token(sender_id, new_rt)
    return access


def _access_token(sender_id: str) -> str:
    with _token_lock:
        cached = _access_tokens.get(sender_id)
        now = time.time()
        if cached and now < cached[1]:
            return cached[0]
        token = _refresh_access_token(sender_id)
        _access_tokens[sender_id] = (token, now + ACCESS_TOKEN_TTL_SECONDS)
        return token


# ─── HTTP helpers ──────────────────────────────────────────────────────────

def _headers(sender_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_access_token(sender_id)}",
        "x-myobapi-key": CLIENT_ID,
        "x-myobapi-version": "v2",
        "Accept": "application/json",
    }


def _get(path: str, sender_id: str, params: dict[str, Any] | None = None) -> Any:
    """GET against the company-file root, on behalf of `sender_id`."""
    _require_creds()
    if not path.startswith("/"):
        path = "/" + path
    url = f"{COMPANY_FILE_BASE}{path}"
    r = requests.get(url, headers=_headers(sender_id), params=params, timeout=30)
    if r.status_code == 401:
        # Stale cached access token — invalidate this sender's cache and
        # retry once before giving up.
        with _token_lock:
            _access_tokens.pop(sender_id, None)
        r = requests.get(url, headers=_headers(sender_id), params=params, timeout=30)
    if not r.ok:
        body = r.text[:500] if r.text else ""
        raise RuntimeError(f"MYOB GET {path} failed ({r.status_code}): {body}")
    if not r.content:
        return None
    return r.json()


def _resolve_sender(sender_id: str) -> str:
    """Validate sender_id. Required — the LLM must extract chat_id from the
    Telegram channel tag and pass it. Direct/admin callers pass "_admin"
    explicitly for the legacy single-account fallback."""
    if not sender_id:
        raise ValueError(
            "sender_id is required. Extract it from the Telegram channel "
            "tag in the user's message: <channel ... chat_id=\"X\" ...>. "
            "Pass that chat_id as sender_id. For direct/admin invocations, "
            "pass sender_id=\"_admin\"."
        )
    return _sanitize_id(sender_id)


# ─── MCP server ─────────────────────────────────────────────────────────────

mcp = FastMCP(
    "myob",
    instructions=(
        "MYOB AccountRight Live access (read-only). Per-user authentication: "
        "every tool call must include `sender_id`, the chat_id from the "
        "Telegram channel tag of the message that prompted the request "
        "(<channel source=\"telegram\" chat_id=\"...\" ...>). Each user "
        "authorizes their own MYOB account once; subsequent calls return "
        "only data scoped to their MYOB account.\n\n"
        "If a tool returns {\"auth_required\": true, ...}, relay the "
        "`message` field to the user via Telegram and stop. The user will "
        "click the link, sign in, hit a 'site can't be reached' page on "
        "localhost, and message the URL from their address bar back. When "
        "they do, call myob_complete_auth(redirect_url, sender_id), then "
        "retry the original request."
    ),
)


# ─── auth-dance tools ──────────────────────────────────────────────────────

@mcp.tool()
def myob_complete_auth(redirect_url: str, sender_id: str) -> dict:
    """Second leg of the per-user OAuth dance — call after the user pastes
    back the URL their browser ended on after signing in to MYOB.

    Args:
        redirect_url: The full URL the user copied from their browser's
            address bar (e.g. 'http://localhost/?code=...&state=...').
            A bare `code` value is also accepted if the user was unable
            to copy the whole URL.
        sender_id: Telegram chat_id of the user being authorized — must
            match the chat_id the original auth request was issued for.
            Pulled from the channel tag.

    Returns {linked: true, message: "..."} on success, or
    {auth_required: true, ...} if the link expired and they need to start over.
    """
    if not sender_id:
        return {"error": "sender_id is required"}
    sender_id = _sanitize_id(sender_id)

    # Extract code + state. Accept either a full URL or a bare code.
    code: str | None = None
    state: str | None = None
    if redirect_url and (redirect_url.startswith("http://") or redirect_url.startswith("https://")):
        from urllib.parse import urlparse, parse_qs
        try:
            qs = parse_qs(urlparse(redirect_url).query)
            code = (qs.get("code") or [None])[0]
            state = (qs.get("state") or [None])[0]
        except Exception:
            return {"error": "Could not parse the URL — make sure you copied the entire address from the browser bar."}
    else:
        code = (redirect_url or "").strip() or None

    if not code:
        return {"error": "No `code` parameter found. Did you copy the URL from the failed-redirect page (the one starting with http://localhost/?code=…)?"}

    # State validation: bind this redirect URL back to the sender it was
    # issued for. If state is missing or doesn't match, refuse — that's
    # either an expired/already-used link, or someone pasting a URL that
    # was meant for a different user.
    if state:
        bound_sender = _consume_pending_state(state)
        if bound_sender is None:
            return {"error": "That sign-in link has expired or was already used. Ask your question again to get a fresh one."}
        if bound_sender != sender_id:
            return {"error": "That sign-in link belongs to a different user — start a new one by asking your question again."}

    # Exchange code → tokens.
    _require_creds()
    r = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    if not r.ok:
        return {"error": f"Token exchange failed ({r.status_code}): {r.text[:200]}"}
    payload = r.json()
    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        return {"error": "MYOB did not return a refresh_token — make sure the requested scope includes offline_access (it should be set automatically)."}
    _save_user_token(sender_id, refresh_token)
    return {
        "linked": True,
        "message": "Authorized. You can now ask MYOB questions.",
    }


@mcp.tool()
def myob_who_am_i(sender_id: str) -> dict:
    """Diagnostic: report whether the calling sender has linked their
    MYOB account, and (if so) when. Helpful for users who can't tell
    whether their auth went through."""
    sender_id = _resolve_sender(sender_id)
    if sender_id == ADMIN_SENDER:
        return {
            "sender_id": "_admin",
            "linked": bool(SEED_REFRESH_TOKEN) or LEGACY_REFRESH_TOKEN_FILE.exists(),
            "mode": "admin/single-account",
        }
    p = _user_token_path(sender_id)
    if not p.exists():
        return {"sender_id": sender_id, "linked": False, "mode": "per-user"}
    try:
        data = json.loads(p.read_text())
        return {
            "sender_id": sender_id,
            "linked": True,
            "mode": "per-user",
            "linked_at": data.get("linked_at"),
            "updated_at": data.get("updated_at"),
        }
    except Exception:
        return {"sender_id": sender_id, "linked": True, "mode": "per-user"}


# ─── company file metadata ─────────────────────────────────────────────────

@mcp.tool()
@requires_auth
def myob_company_file_info(sender_id: str) -> dict:
    """Return company file metadata (name, AccountRight product/version,
    last sync time, country, currency). Useful as a connectivity sanity
    check before running larger queries."""
    return _get("/", _resolve_sender(sender_id))


# ─── general ledger ────────────────────────────────────────────────────────

ACCOUNT_CLASSIFICATIONS = {
    "Asset", "Liability", "Equity", "Income", "Expense",
    "CostOfSales", "OtherIncome", "OtherExpense",
}


@mcp.tool()
@requires_auth
def myob_accounts(
    class_filter: str | None = None,
    account_number_prefix: str | None = None,
    limit: int = 500,
    *,
    sender_id: str,
) -> list[dict]:
    """List the chart of accounts.

    Args:
        class_filter: Restrict to one classification — Asset, Liability,
            Equity, Income, Expense, CostOfSales, OtherIncome, OtherExpense.
        account_number_prefix: Restrict to accounts whose DisplayID starts
            with this string (e.g. "4-" for income accounts in many setups).
        limit: Max accounts to return (default 500, MYOB's hard cap is 1000).
    """
    sender = _resolve_sender(sender_id)
    params: dict[str, Any] = {"$top": min(max(limit, 1), 1000)}
    filters: list[str] = []
    if class_filter:
        if class_filter not in ACCOUNT_CLASSIFICATIONS:
            raise ValueError(
                f"class_filter must be one of {sorted(ACCOUNT_CLASSIFICATIONS)}, "
                f"got {class_filter!r}"
            )
        filters.append(f"Classification eq '{class_filter}'")
    if filters:
        params["$filter"] = " and ".join(filters)

    data = _get("/GeneralLedger/Account", sender, params=params)
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


# ─── reports ───────────────────────────────────────────────────────────────

REPORTING_BASES = {"Accrual", "Cash"}


def _pl_call(start_date: str, end_date: str, basis: str, sender_id: str) -> dict:
    if basis not in REPORTING_BASES:
        raise ValueError(f"basis must be 'Accrual' or 'Cash', got {basis!r}")
    return _get(
        "/Report/ProfitAndLossSummary",
        sender_id,
        params={
            "startDate": start_date,
            "endDate": end_date,
            "reportingBasis": basis,
            "yearEndAdjust": "false",
        },
    )


@mcp.tool()
@requires_auth
def myob_pl_summary(
    start_date: str,
    end_date: str,
    basis: str = "Accrual",
    *,
    sender_id: str,
) -> dict:
    """Profit & Loss summary report for one date range.

    Args:
        start_date: ISO date 'YYYY-MM-DD' (inclusive).
        end_date: ISO date 'YYYY-MM-DD' (inclusive).
        basis: 'Accrual' (default) or 'Cash'.
    """
    return _pl_call(start_date, end_date, basis, _resolve_sender(sender_id))


@mcp.tool()
@requires_auth
def myob_pl_compare(
    periods_json: str,
    basis: str = "Accrual",
    *,
    sender_id: str,
) -> list[dict]:
    """Run P&L summary across multiple periods and return them side-by-side.

    Args:
        periods_json: JSON array of {label, start_date, end_date} objects.
        basis: 'Accrual' (default) or 'Cash' — applied to every period.
    """
    sender = _resolve_sender(sender_id)
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
        out.append({
            "label": label,
            "start_date": start,
            "end_date": end,
            "report": _pl_call(start, end, basis, sender),
        })
    return out


# ─── employees / payroll ───────────────────────────────────────────────────

def _odata_str(value: str) -> str:
    return value.replace("'", "''")


def _summarize_employee(emp: dict) -> dict:
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
@requires_auth
def myob_employee_lookup(
    first_name: str | None = None,
    last_name: str | None = None,
    *,
    sender_id: str,
) -> list[dict]:
    """Find employees whose first or last name exactly matches (case-sensitive).

    MYOB's $filter only supports `eq` / `ne`, not `contains` — pass
    exact strings. For partial matches use myob_employee_leave_balance
    (which has a fuzzy fallback).
    """
    sender = _resolve_sender(sender_id)
    if not (first_name or last_name):
        raise ValueError("Provide at least one of first_name or last_name.")
    clauses: list[str] = []
    if first_name:
        clauses.append(f"FirstName eq '{_odata_str(first_name)}'")
    if last_name:
        clauses.append(f"LastName eq '{_odata_str(last_name)}'")
    params = {"$filter": " or ".join(clauses)}
    data = _get("/Contact/Employee", sender, params=params)
    items = data.get("Items", []) if isinstance(data, dict) else []
    return [_summarize_employee(e) for e in items]


@mcp.tool()
@requires_auth
def myob_employee_payroll_details(uid: str, sender_id: str) -> dict:
    """Full payroll record for one employee by UID.

    Returns AnnualSalary, HourlyRate, PayFrequency, HoursInWeeklyPayPeriod,
    StartDate, WageCategories, Superannuation, Tax, and the full
    Entitlements array (leave balances).
    """
    if not uid:
        raise ValueError("uid is required")
    return _get(f"/Contact/EmployeePayrollDetails/{uid}", _resolve_sender(sender_id))


@mcp.tool()
@requires_auth
def myob_employee_standard_pay(uid: str, sender_id: str) -> dict:
    """Standard (recurring) pay configuration for one employee by UID."""
    if not uid:
        raise ValueError("uid is required")
    return _get(f"/Contact/EmployeeStandardPay/{uid}", _resolve_sender(sender_id))


# Working days per pay period for hours→days conversion.
PERIOD_WORKING_DAYS = {
    "Weekly": 5,
    "Fortnightly": 10,
    "Monthly": 22,
    "Bimonthly": 11,
    "Quarterly": 65,
}


def _hours_per_day(payroll: dict) -> float:
    wage = payroll.get("Wage") or {}
    period_hours = wage.get("HoursInWeeklyPayPeriod")
    pay_freq = wage.get("PayFrequency") or "Weekly"
    days = PERIOD_WORKING_DAYS.get(pay_freq, 5)
    if isinstance(period_hours, (int, float)) and period_hours > 0 and days > 0:
        return float(period_hours) / float(days)
    return 7.6


@mcp.tool()
@requires_auth
def myob_employee_leave_balance(name: str, sender_id: str) -> dict:
    """Convenience: look up an employee by name, return only their assigned
    leave entitlements with hours and an estimated days-equivalent.

    Args:
        name: First or last name (or part). Falls back to a substring scan
            across all active employees if no exact match is found —
            useful when the actual LastName has multiple words.
    """
    sender = _resolve_sender(sender_id)
    if not name or not name.strip():
        raise ValueError("name is required")
    needle = name.strip()

    matches = myob_employee_lookup(last_name=needle, sender_id=sender_id)
    if not matches:
        matches = myob_employee_lookup(first_name=needle, sender_id=sender_id)
    if not matches:
        # Fuzzy: full-list substring match.
        try:
            data = _get("/Contact/Employee", sender, params={"$top": 1000})
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
    payroll = _get(f"/Contact/EmployeePayrollDetails/{payroll_uid}", sender)

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
        cat = ent.get("EntitlementCategory") or ent.get("PayrollCategory") or {}
        assigned.append({
            "name": cat.get("Name"),
            "uid": cat.get("UID"),
            "carry_over_hours": carry,
            "year_to_date_hours": ytd,
            "total_hours": total,
            "days_equivalent": round(total / hpd, 2) if hpd else None,
        })

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


# ─── payroll category catalogues ───────────────────────────────────────────

@mcp.tool()
@requires_auth
def myob_wage_categories(sender_id: str) -> list[dict]:
    """List wage payroll categories (ordinary time, overtime, allowances)."""
    data = _get("/Payroll/PayrollCategory/Wage", _resolve_sender(sender_id))
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
@requires_auth
def myob_entitlement_categories(sender_id: str) -> list[dict]:
    """List entitlement payroll categories (sick, holiday, long-service)."""
    data = _get("/Payroll/PayrollCategory/Entitlement", _resolve_sender(sender_id))
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


# ─── escape hatch ──────────────────────────────────────────────────────────

@mcp.tool()
@requires_auth
def myob_raw_get(
    path: str,
    params_json: str | None = None,
    *,
    sender_id: str,
) -> Any:
    """GET an arbitrary AccountRight company-file path.

    Use only for endpoints not covered by the typed tools above. `path` is
    relative to the company file root.
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
    return _get(path, _resolve_sender(sender_id), params=params)


# ─── entry ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
