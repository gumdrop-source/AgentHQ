"""Xero (accounting) MCP server — read-only, per-user auth, explicit org pick.

Each Telegram sender authorizes once by signing in to their own Xero
account; their refresh_token is cached separately so queries return
only data their Xero account is scoped for.

Two-stage prompt UX: a sender must (1) sign in, then (2) explicitly
pick which connected Xero organisation subsequent queries run against
— even if they're only connected to one. The decorator stack on each
data tool is:

    @mcp.tool()
    @requires_auth      # outer: catches AuthRequired → sign-in prompt
    @requires_org       # inner: catches OrgRequired → org-picker prompt

Identity: every tool function takes a `sender_id` parameter. The
agent's persona/system prompt instructs Claude to extract this from
the Telegram channel tag (`<channel ... chat_id="X" ...>`). Soft-trust
caveat applies — same as MYOB/Tanda; see tools/myob/server.py for the
longer note.

Xero specifics:
  - OAuth token endpoint takes HTTP Basic auth in the Authorization
    header (NOT client_id/secret in the form body).
  - Refresh tokens rotate (single-use). Old token expires once a new
    one is issued. Inactive refresh tokens expire at 30 days.
  - Access tokens last 30 minutes; refresh at 25.
  - Every API call needs Xero-tenant-id header — the picked org's
    tenantId from /connections.
"""

from __future__ import annotations

import base64
import functools
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, get_origin, get_type_hints

import requests
from mcp.server.fastmcp import FastMCP

# ─── credentials ────────────────────────────────────────────────────────────

CLIENT_ID = os.environ.get("XERO_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("XERO_CLIENT_SECRET", "")
SEED_REFRESH_TOKEN = os.environ.get("XERO_REFRESH_TOKEN", "")  # admin-seed (single-account fallback)

HOME = Path(os.environ.get("HOME", "/tmp"))
PER_USER_TOKEN_DIR = HOME / "xero_tokens"
PER_USER_ORG_DIR = HOME / "xero_active_org"
PENDING_AUTH_DIR = HOME / "xero_pending_auth"
LEGACY_REFRESH_TOKEN_FILE = HOME / "xero_refresh_token"

OAUTH_AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
OAUTH_TOKEN_URL = "https://identity.xero.com/connect/token"
OAUTH_REDIRECT_URI = "http://localhost"
# Broad scopes. The granular `.read` flavors (accounting.contacts.read etc)
# require an opt-in that Web-app registrations don't get by default —
# requesting them yields "unauthorized_client / Invalid scope for client"
# at the authorize endpoint. The broad scopes have been available for every
# OAuth2 app since 2020, so they're the safe default. Token has write
# capability but our tool code only ever calls GETs.
OAUTH_SCOPE = (
    "offline_access "
    "accounting.contacts "
    "accounting.transactions "
    "accounting.settings "
    "accounting.reports.read"
)

CONNECTIONS_URL = "https://api.xero.com/connections"
API_BASE = "https://api.xero.com/api.xro/2.0"

ACCESS_TOKEN_TTL_SECONDS = 25 * 60        # Xero access tokens last 30 min, refresh at 25
PENDING_AUTH_TTL_SECONDS = 10 * 60

ADMIN_SENDER = "_admin"


def _require_creds() -> None:
    missing = [
        name for name, val in [
            ("XERO_CLIENT_ID", CLIENT_ID),
            ("XERO_CLIENT_SECRET", CLIENT_SECRET),
        ] if not val
    ]
    if missing:
        raise RuntimeError(
            f"Xero platform credentials missing: {', '.join(missing)}. "
            "The operator must activate the xero integration in agent-control "
            "(which provisions the encrypted credentials and reloads the agent service)."
        )


def _basic_auth_header() -> str:
    """HTTP Basic auth header for the Xero token endpoint."""
    raw = f"{CLIENT_ID}:{CLIENT_SECRET}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


# ─── id sanitization ───────────────────────────────────────────────────────

def _sanitize_id(sender_id: str) -> str:
    s = "".join(ch for ch in sender_id if ch.isalnum() or ch in "_-")
    if not s:
        raise ValueError(f"empty/invalid sender_id: {sender_id!r}")
    return s


def _resolve_sender(sender_id: str) -> str:
    if not sender_id:
        raise ValueError(
            "sender_id is required. Extract it from the Telegram channel "
            "tag in the user's message: <channel ... chat_id=\"X\" ...>. "
            "Pass that chat_id as sender_id. For direct/admin invocations, "
            "pass sender_id=\"_admin\"."
        )
    return _sanitize_id(sender_id)


# ─── per-user refresh-token storage ────────────────────────────────────────

def _user_token_path(sender_id: str) -> Path:
    return PER_USER_TOKEN_DIR / f"{_sanitize_id(sender_id)}.json"


def _load_user_token(sender_id: str) -> str | None:
    p = _user_token_path(sender_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return data.get("refresh_token")
    except (OSError, json.JSONDecodeError):
        return None


def _save_user_token(sender_id: str, refresh_token: str) -> None:
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
    if target.exists():
        try:
            existing = json.loads(target.read_text())
            for k in ("linked_at",):
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
    if sender_id == ADMIN_SENDER:
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
    if sender_id == ADMIN_SENDER:
        try:
            tmp = LEGACY_REFRESH_TOKEN_FILE.with_suffix(".tmp")
            tmp.write_text(refresh_token)
            tmp.chmod(0o600)
            tmp.replace(LEGACY_REFRESH_TOKEN_FILE)
        except OSError:
            pass
        return
    _save_user_token(sender_id, refresh_token)


# ─── per-user active-org cache ─────────────────────────────────────────────

def _user_org_path(sender_id: str) -> Path:
    return PER_USER_ORG_DIR / f"{_sanitize_id(sender_id)}.json"


def _load_active_org(sender_id: str) -> dict | None:
    p = _user_org_path(sender_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _save_active_org(sender_id: str, tenant_id: str, name: str | None) -> None:
    PER_USER_ORG_DIR.mkdir(mode=0o700, exist_ok=True)
    try:
        PER_USER_ORG_DIR.chmod(0o700)
    except OSError:
        pass
    target = _user_org_path(sender_id)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "tenant_id": tenant_id,
        "name": name,
        "picked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }))
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(target)


def _clear_active_org(sender_id: str) -> None:
    try:
        _user_org_path(sender_id).unlink(missing_ok=True)
    except OSError:
        pass


# ─── pending-auth state ────────────────────────────────────────────────────

def _save_pending_state(state: str, sender_id: str) -> None:
    PENDING_AUTH_DIR.mkdir(mode=0o700, exist_ok=True)
    try:
        PENDING_AUTH_DIR.chmod(0o700)
    except OSError:
        pass
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


# ─── auth + org-pick exceptions ────────────────────────────────────────────

class AuthRequired(Exception):
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
        super().__init__(f"Xero sign-in required for sender {sender_id}")


class OrgRequired(Exception):
    """Raised when the sender is authenticated but hasn't picked an active
    Xero organisation yet. The wrapper decorator fetches /connections and
    surfaces the options to the LLM so the user can choose."""

    def __init__(self, sender_id: str, options: list[dict]) -> None:
        self.sender_id = sender_id
        self.options = options
        super().__init__(f"Active Xero org required for sender {sender_id}")


def requires_auth(fn: Callable) -> Callable:
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
                    "To do that I need access to your Xero account. "
                    f"Sign in here: {e.authorize_url}\n\n"
                    "After you sign in (and pick which Xero organisations to share), "
                    "Xero will redirect to a 'site can't be reached' page on "
                    "http://localhost — that's expected. Copy the entire URL from "
                    "your browser's address bar and send it back to me, then ask "
                    "your question again."
                ),
                "authorize_url": e.authorize_url,
            }
            return [payload] if list_returning else payload
    return wrapper


def requires_org(fn: Callable) -> Callable:
    """Catch OrgRequired and surface the connected-org list to the LLM.

    Sits inside @requires_auth so AuthRequired (no token) takes precedence
    over OrgRequired (token but no active org chosen).
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
        except OrgRequired as e:
            if not e.options:
                payload = {
                    "org_required": True,
                    "options": [],
                    "message": (
                        "Your Xero sign-in succeeded but no organisations are "
                        "connected to this app. Either re-run sign-in (and tick "
                        "at least one organisation on the Xero consent screen) "
                        "or grant the AgentHQ app access to an organisation in "
                        "Xero settings."
                    ),
                }
            else:
                lines = [
                    f"  {i + 1}. {o.get('name')}  (tenant_id: {o.get('tenant_id')})"
                    for i, o in enumerate(e.options)
                ]
                payload = {
                    "org_required": True,
                    "options": e.options,
                    "message": (
                        "Which Xero organisation should I run this against?\n\n"
                        + "\n".join(lines)
                        + "\n\nReply with the name (or number) and I'll lock it "
                        "in for future questions, or tell me 'use <name>'."
                    ),
                }
            return [payload] if list_returning else payload
    return wrapper


# ─── access-token cache ────────────────────────────────────────────────────

_token_lock = threading.Lock()
_access_tokens: dict[str, tuple[str, float]] = {}


def _refresh_access_token(sender_id: str) -> str:
    """Exchange this sender's refresh_token for a fresh access_token.
    Xero's refresh tokens rotate (single-use) — persist the new one."""
    _require_creds()
    rt = _resolve_refresh_token(sender_id)
    r = requests.post(
        OAUTH_TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": rt,
        },
        timeout=15,
    )
    if r.status_code != 200:
        body = r.text[:300] if r.text else ""
        if "invalid_grant" in body and sender_id != ADMIN_SENDER:
            try:
                _user_token_path(sender_id).unlink(missing_ok=True)
            except OSError:
                pass
            # Active org cache is keyed to the sender, not the token, but
            # the user is going to re-pick on re-auth anyway and orgs may
            # have changed — clear it so we re-run the picker fresh.
            _clear_active_org(sender_id)
            raise AuthRequired(sender_id) from None
        raise RuntimeError(f"Xero token refresh failed ({r.status_code}): {body}")
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


# ─── connections + active-org resolution ───────────────────────────────────

def _list_connections(sender_id: str) -> list[dict]:
    """Hit /connections to enumerate the orgs this sender has connected.
    Each entry has tenantId, tenantType, tenantName, createdDateTime, etc."""
    _require_creds()
    r = requests.get(
        CONNECTIONS_URL,
        headers={
            "Authorization": f"Bearer {_access_token(sender_id)}",
            "Accept": "application/json",
        },
        timeout=15,
    )
    if r.status_code == 401:
        with _token_lock:
            _access_tokens.pop(sender_id, None)
        r = requests.get(
            CONNECTIONS_URL,
            headers={
                "Authorization": f"Bearer {_access_token(sender_id)}",
                "Accept": "application/json",
            },
            timeout=15,
        )
    if not r.ok:
        body = r.text[:300] if r.text else ""
        raise RuntimeError(f"Xero /connections failed ({r.status_code}): {body}")
    return r.json() or []


def _summarize_connection(c: dict) -> dict:
    return {
        "tenant_id": c.get("tenantId"),
        "name": c.get("tenantName"),
        "type": c.get("tenantType"),
        "connection_id": c.get("id"),
        "created_at": c.get("createdDateTime"),
    }


def _require_active_tenant(sender_id: str) -> str:
    """Return the picked tenant_id for this sender. Raise OrgRequired with
    the connected-org options if none is picked (yet, or anymore)."""
    active = _load_active_org(sender_id)
    options = [_summarize_connection(c) for c in _list_connections(sender_id)]

    # If they had a picked org, validate it's still in /connections — the
    # operator/owner may have revoked the connection from inside Xero. If
    # the previously-picked tenant is gone, fall through to the picker.
    if active and active.get("tenant_id"):
        if any(o.get("tenant_id") == active["tenant_id"] for o in options):
            return active["tenant_id"]
        _clear_active_org(sender_id)

    raise OrgRequired(sender_id, options)


# ─── HTTP helpers ──────────────────────────────────────────────────────────

def _api_headers(sender_id: str, tenant_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_access_token(sender_id)}",
        "Xero-tenant-id": tenant_id,
        "Accept": "application/json",
    }


def _get(path: str, sender_id: str, params: dict[str, Any] | None = None) -> Any:
    """GET against the Xero accounting API on behalf of `sender_id`,
    against the org they previously picked."""
    _require_creds()
    tenant_id = _require_active_tenant(sender_id)
    if not path.startswith("/"):
        path = "/" + path
    url = f"{API_BASE}{path}"
    r = requests.get(url, headers=_api_headers(sender_id, tenant_id), params=params, timeout=30)
    if r.status_code == 401:
        with _token_lock:
            _access_tokens.pop(sender_id, None)
        r = requests.get(url, headers=_api_headers(sender_id, tenant_id), params=params, timeout=30)
    if not r.ok:
        body = r.text[:500] if r.text else ""
        raise RuntimeError(f"Xero GET {path} failed ({r.status_code}): {body}")
    if not r.content:
        return None
    try:
        return r.json()
    except ValueError:
        return r.text


# ─── MCP server ─────────────────────────────────────────────────────────────

mcp = FastMCP(
    "xero",
    instructions=(
        "Xero accounting access (read-only). Per-user authentication, plus "
        "an explicit per-user organisation pick — every tool call must "
        "include `sender_id` (chat_id from the Telegram channel tag).\n\n"
        "Two-stage prompt protocol:\n"
        "1. {\"auth_required\": true, ...}: relay the `message` field to the "
        "user via Telegram and stop. They sign in, paste back the redirect "
        "URL, you call xero_complete_auth(redirect_url, sender_id), then "
        "retry the original request.\n"
        "2. {\"org_required\": true, options: [...]}: relay the `message` "
        "field listing connected organisations. The user names one (or "
        "gives the number); you call xero_set_active_org(tenant_id, "
        "sender_id), then retry the original request.\n\n"
        "After org is picked, all data tools run against that organisation "
        "until the user asks to switch. To switch, call xero_set_active_org "
        "again with a different tenant_id."
    ),
)


# ─── auth-dance tools ──────────────────────────────────────────────────────

@mcp.tool()
def xero_complete_auth(redirect_url: str, sender_id: str) -> dict:
    """Second leg of the per-user OAuth dance — call after the user pastes
    back the URL their browser ended on after signing in to Xero.

    Args:
        redirect_url: The full URL the user copied from their browser's
            address bar (e.g. 'http://localhost/?code=...&state=...').
            A bare `code` value is also accepted if the user couldn't
            copy the whole URL.
        sender_id: Telegram chat_id of the user being authorized — must
            match the chat_id the original auth request was issued for.

    Returns {linked: true, message: "..."} on success.
    """
    if not sender_id:
        return {"error": "sender_id is required"}
    sender_id = _sanitize_id(sender_id)

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

    if state:
        bound_sender = _consume_pending_state(state)
        if bound_sender is None:
            return {"error": "That sign-in link has expired or was already used. Ask your question again to get a fresh one."}
        if bound_sender != sender_id:
            return {"error": "That sign-in link belongs to a different user — start a new one by asking your question again."}

    _require_creds()
    r = requests.post(
        OAUTH_TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": OAUTH_REDIRECT_URI,
        },
        timeout=15,
    )
    if not r.ok:
        return {"error": f"Token exchange failed ({r.status_code}): {r.text[:200]}"}
    payload = r.json()
    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        return {"error": "Xero did not return a refresh_token — make sure the requested scope includes offline_access (it should be set automatically)."}
    _save_user_token(sender_id, refresh_token)
    # Force the picker on next data call by clearing any stale active org.
    _clear_active_org(sender_id)
    return {
        "linked": True,
        "message": (
            "Authorized. One more step: tell me which Xero organisation "
            "to run queries against. Ask a question and I'll list your "
            "connected orgs so you can pick."
        ),
    }


@mcp.tool()
def xero_who_am_i(sender_id: str) -> dict:
    """Diagnostic: report whether the calling sender has linked their
    Xero account, and which organisation (if any) is currently active."""
    sender_id = _resolve_sender(sender_id)
    if sender_id == ADMIN_SENDER:
        linked = bool(SEED_REFRESH_TOKEN) or LEGACY_REFRESH_TOKEN_FILE.exists()
        active = _load_active_org(sender_id)
        return {
            "sender_id": "_admin",
            "linked": linked,
            "mode": "admin/single-account",
            "active_org": active,
        }
    p = _user_token_path(sender_id)
    linked = p.exists()
    active = _load_active_org(sender_id)
    out: dict[str, Any] = {
        "sender_id": sender_id,
        "linked": linked,
        "mode": "per-user",
        "active_org": active,
    }
    if linked:
        try:
            data = json.loads(p.read_text())
            out["linked_at"] = data.get("linked_at")
            out["updated_at"] = data.get("updated_at")
        except Exception:
            pass
    return out


@mcp.tool()
@requires_auth
def xero_organisations(sender_id: str) -> list[dict]:
    """List the Xero organisations the linked user is connected to.

    Use this when the user asks 'which Xero orgs am I connected to?',
    or to remind yourself of the tenant_ids before calling
    xero_set_active_org.
    """
    sender = _resolve_sender(sender_id)
    return [_summarize_connection(c) for c in _list_connections(sender)]


@mcp.tool()
@requires_auth
def xero_set_active_org(tenant_id: str, sender_id: str) -> dict:
    """Pick which connected Xero organisation subsequent data queries
    should run against. Pass the `tenant_id` from xero_organisations
    (or the option list returned in an `org_required` response).

    Required before any data tool will work, even if there's only one
    connected org.
    """
    sender = _resolve_sender(sender_id)
    if not tenant_id:
        raise ValueError("tenant_id is required (get it from xero_organisations).")
    options = [_summarize_connection(c) for c in _list_connections(sender)]
    match = next((o for o in options if o.get("tenant_id") == tenant_id), None)
    if not match:
        return {
            "error": f"tenant_id {tenant_id!r} is not in your connected orgs. "
                     f"Call xero_organisations to see what's available.",
            "options": options,
        }
    _save_active_org(sender, tenant_id, match.get("name"))
    return {
        "active_org": {"tenant_id": tenant_id, "name": match.get("name")},
        "message": f"Now using Xero organisation: {match.get('name')}.",
    }


# ─── organisation metadata ─────────────────────────────────────────────────

@mcp.tool()
@requires_auth
@requires_org
def xero_organisation_info(sender_id: str) -> dict:
    """Active organisation metadata: legal name, base currency, country,
    financial year end, time zone. Useful as a connectivity sanity check
    before running larger queries."""
    data = _get("/Organisation", _resolve_sender(sender_id))
    orgs = (data or {}).get("Organisations") or []
    if not orgs:
        return {"error": "No organisation returned by /Organisation.", "raw": data}
    o = orgs[0]
    return {
        "name": o.get("Name"),
        "legal_name": o.get("LegalName"),
        "short_code": o.get("ShortCode"),
        "country_code": o.get("CountryCode"),
        "base_currency": o.get("BaseCurrency"),
        "timezone": o.get("Timezone"),
        "financial_year_end_month": o.get("FinancialYearEndMonth"),
        "financial_year_end_day": o.get("FinancialYearEndDay"),
        "organisation_status": o.get("OrganisationStatus"),
    }


# ─── contacts ──────────────────────────────────────────────────────────────

def _xero_str(value: str) -> str:
    """Escape a string for embedding in a Xero `where` clause."""
    return value.replace('"', '\\"')


def _summarize_contact(c: dict) -> dict:
    return {
        "id": c.get("ContactID"),
        "name": c.get("Name"),
        "email": c.get("EmailAddress"),
        "is_supplier": c.get("IsSupplier"),
        "is_customer": c.get("IsCustomer"),
        "status": c.get("ContactStatus"),
        "account_number": c.get("AccountNumber"),
    }


@mcp.tool()
@requires_auth
@requires_org
def xero_contacts(query: str, sender_id: str, limit: int = 100) -> list[dict]:
    """Search contacts (customers and suppliers) by name fragment.

    Args:
        query: Substring to match against the contact's Name (case-insensitive).
        limit: Max results (default 100, Xero pages at 100).
    """
    sender = _resolve_sender(sender_id)
    if not query or not query.strip():
        raise ValueError("query is required")
    q = _xero_str(query.strip())
    params = {"where": f'Name!=null && Name.Contains("{q}")'}
    data = _get("/Contacts", sender, params=params)
    items = (data or {}).get("Contacts") or []
    return [_summarize_contact(c) for c in items[: max(1, min(limit, 100))]]


# ─── invoices (AR) and bills (AP) ──────────────────────────────────────────

INVOICE_STATUSES = {"DRAFT", "SUBMITTED", "AUTHORISED", "PAID", "VOIDED", "DELETED"}


def _summarize_invoice(inv: dict) -> dict:
    contact = inv.get("Contact") or {}
    return {
        "id": inv.get("InvoiceID"),
        "number": inv.get("InvoiceNumber"),
        "type": inv.get("Type"),
        "status": inv.get("Status"),
        "date": inv.get("DateString") or inv.get("Date"),
        "due_date": inv.get("DueDateString") or inv.get("DueDate"),
        "contact_id": contact.get("ContactID"),
        "contact_name": contact.get("Name"),
        "currency": inv.get("CurrencyCode"),
        "subtotal": inv.get("SubTotal"),
        "tax": inv.get("TotalTax"),
        "total": inv.get("Total"),
        "amount_due": inv.get("AmountDue"),
        "amount_paid": inv.get("AmountPaid"),
        "reference": inv.get("Reference"),
    }


def _list_invoices(
    sender: str,
    invoice_type: str,
    start_date: str | None,
    end_date: str | None,
    status: str | None,
    contact_id: str | None,
    limit: int,
) -> list[dict]:
    where_clauses = [f'Type=="{invoice_type}"']
    if start_date:
        where_clauses.append(f'Date>=DateTime({start_date.replace("-", ",")})')
    if end_date:
        where_clauses.append(f'Date<=DateTime({end_date.replace("-", ",")})')
    if status:
        if status.upper() not in INVOICE_STATUSES:
            raise ValueError(f"status must be one of {sorted(INVOICE_STATUSES)}, got {status!r}")
        where_clauses.append(f'Status=="{status.upper()}"')
    if contact_id:
        where_clauses.append(f'Contact.ContactID==Guid("{contact_id}")')
    params: dict[str, Any] = {
        "where": " && ".join(where_clauses),
        "order": "Date DESC",
    }
    data = _get("/Invoices", sender, params=params)
    items = (data or {}).get("Invoices") or []
    return [_summarize_invoice(i) for i in items[: max(1, min(limit, 1000))]]


@mcp.tool()
@requires_auth
@requires_org
def xero_invoices(
    start_date: str | None = None,
    end_date: str | None = None,
    status: str | None = None,
    contact_id: str | None = None,
    limit: int = 200,
    *,
    sender_id: str,
) -> list[dict]:
    """List sales invoices (Type=ACCREC) — accounts receivable.

    Args:
        start_date: ISO date 'YYYY-MM-DD' lower bound on invoice date.
        end_date: ISO date 'YYYY-MM-DD' upper bound on invoice date.
        status: One of DRAFT, SUBMITTED, AUTHORISED, PAID, VOIDED, DELETED.
        contact_id: Restrict to one customer (ContactID GUID).
        limit: Max invoices to return (default 200).
    """
    return _list_invoices(
        _resolve_sender(sender_id), "ACCREC",
        start_date, end_date, status, contact_id, limit,
    )


@mcp.tool()
@requires_auth
@requires_org
def xero_bills(
    start_date: str | None = None,
    end_date: str | None = None,
    status: str | None = None,
    contact_id: str | None = None,
    limit: int = 200,
    *,
    sender_id: str,
) -> list[dict]:
    """List purchase bills (Type=ACCPAY) — accounts payable.

    Args:
        start_date: ISO date 'YYYY-MM-DD' lower bound on bill date.
        end_date: ISO date 'YYYY-MM-DD' upper bound on bill date.
        status: One of DRAFT, SUBMITTED, AUTHORISED, PAID, VOIDED, DELETED.
        contact_id: Restrict to one supplier (ContactID GUID).
        limit: Max bills to return (default 200).
    """
    return _list_invoices(
        _resolve_sender(sender_id), "ACCPAY",
        start_date, end_date, status, contact_id, limit,
    )


# ─── chart of accounts ─────────────────────────────────────────────────────

ACCOUNT_CLASSES = {"ASSET", "EQUITY", "EXPENSE", "LIABILITY", "REVENUE"}


@mcp.tool()
@requires_auth
@requires_org
def xero_accounts(
    class_filter: str | None = None,
    code_prefix: str | None = None,
    *,
    sender_id: str,
) -> list[dict]:
    """List the chart of accounts.

    Args:
        class_filter: Restrict to one class — ASSET, LIABILITY, EQUITY,
            REVENUE, EXPENSE.
        code_prefix: Restrict to accounts whose Code starts with this string
            (e.g. '2-' for liability accounts in many setups). Applied
            client-side after fetching.
    """
    sender = _resolve_sender(sender_id)
    params: dict[str, Any] = {}
    if class_filter:
        if class_filter.upper() not in ACCOUNT_CLASSES:
            raise ValueError(f"class_filter must be one of {sorted(ACCOUNT_CLASSES)}, got {class_filter!r}")
        params["where"] = f'Class=="{class_filter.upper()}"'
    data = _get("/Accounts", sender, params=params)
    items = (data or {}).get("Accounts") or []
    if code_prefix:
        items = [a for a in items if (a.get("Code") or "").startswith(code_prefix)]
    return [
        {
            "id": a.get("AccountID"),
            "code": a.get("Code"),
            "name": a.get("Name"),
            "type": a.get("Type"),
            "class": a.get("Class"),
            "tax_type": a.get("TaxType"),
            "status": a.get("Status"),
            "description": a.get("Description"),
            "show_in_expense_claims": a.get("ShowInExpenseClaims"),
            "system_account": a.get("SystemAccount"),
        }
        for a in items
    ]


# ─── reports ───────────────────────────────────────────────────────────────

@mcp.tool()
@requires_auth
@requires_org
def xero_pl(
    from_date: str,
    to_date: str,
    *,
    sender_id: str,
) -> dict:
    """Profit & Loss summary report for one date range.

    Args:
        from_date: ISO date 'YYYY-MM-DD' (inclusive).
        to_date: ISO date 'YYYY-MM-DD' (inclusive).
    """
    if not (from_date and to_date):
        raise ValueError("from_date and to_date are both required (YYYY-MM-DD).")
    return _get(
        "/Reports/ProfitAndLoss",
        _resolve_sender(sender_id),
        params={"fromDate": from_date, "toDate": to_date},
    )


# ─── escape hatch ──────────────────────────────────────────────────────────

@mcp.tool()
@requires_auth
@requires_org
def xero_raw_get(
    path: str,
    params_json: str | None = None,
    *,
    sender_id: str,
) -> Any:
    """GET an arbitrary Xero accounting API path.

    Use only for endpoints not covered by the typed tools above. `path` is
    relative to https://api.xero.com/api.xro/2.0.
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
