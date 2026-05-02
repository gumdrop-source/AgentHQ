"""Tanda (workforce management) MCP server — read-only, per-user auth.

Each Telegram sender authorizes once by signing in to their own Tanda
account; their refresh_token is cached separately so queries return only
data their Tanda account is scoped for (their own roster, their own
leave, plus anything their role grants — managers see their team, etc).

Identity: every tool function takes a `sender_id` parameter. The
agent's persona/system prompt instructs Claude to extract this from
the Telegram channel tag (`<channel ... chat_id="X" ...>`) on every
tool call. NB: the LLM passing `sender_id` is soft trust — same caveat
as the MYOB tool. See tools/myob/server.py for a longer note.

Auth dance (mirrors MYOB):
  1. Tool sees no token for this sender → raises AuthRequired
  2. The @requires_auth decorator turns that into a structured response
     with the authorize URL the LLM relays to the user.
  3. User signs in to Tanda; redirect lands on http://localhost (which
     fails in the browser by design); user copies the URL and sends it
     back to the bot.
  4. LLM calls tanda_complete_auth(redirect_url, sender_id) to finish.

Tanda OAuth quirk: the refresh_token grant requires `redirect_uri` in
the POST body (most providers don't). Refresh tokens rotate on every
use and are single-use, so we persist the new one immediately.
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

CLIENT_ID = os.environ.get("TANDA_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("TANDA_CLIENT_SECRET", "")
SEED_REFRESH_TOKEN = os.environ.get("TANDA_REFRESH_TOKEN", "")  # admin-seed (legacy single-account)

HOME = Path(os.environ.get("HOME", "/tmp"))
PER_USER_TOKEN_DIR = HOME / "tanda_tokens"
PENDING_AUTH_DIR = HOME / "tanda_pending_auth"
LEGACY_REFRESH_TOKEN_FILE = HOME / "tanda_refresh_token"

OAUTH_AUTHORIZE_URL = "https://my.tanda.co/api/oauth/authorize"
OAUTH_TOKEN_URL = "https://my.tanda.co/api/oauth/token"
OAUTH_REDIRECT_URI = "http://localhost"
OAUTH_SCOPE = "me user roster timesheet leave cost"

API_BASE = "https://my.tanda.co/api/v2"

ACCESS_TOKEN_TTL_SECONDS = 110 * 60        # Tanda authcode tokens last 2h, refresh at 1h50
PENDING_AUTH_TTL_SECONDS = 10 * 60         # auth-flow links expire after 10 min

ADMIN_SENDER = "_admin"


def _require_creds() -> None:
    missing = [
        name
        for name, val in [
            ("TANDA_CLIENT_ID", CLIENT_ID),
            ("TANDA_CLIENT_SECRET", CLIENT_SECRET),
        ]
        if not val
    ]
    if missing:
        raise RuntimeError(
            f"Tanda platform credentials missing: {', '.join(missing)}. "
            "The operator must activate the tanda integration in agent-control "
            "(which provisions the encrypted credentials and reloads the agent service)."
        )


# ─── per-user token storage ────────────────────────────────────────────────

def _sanitize_id(sender_id: str) -> str:
    s = "".join(ch for ch in sender_id if ch.isalnum() or ch in "_-")
    if not s:
        raise ValueError(f"empty/invalid sender_id: {sender_id!r}")
    return s


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


# ─── auth ──────────────────────────────────────────────────────────────────

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
        super().__init__(f"Tanda sign-in required for sender {sender_id}")


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
                    "To do that I need access to your Tanda account. "
                    f"Sign in here: {e.authorize_url}\n\n"
                    "After you sign in, Tanda will redirect to a 'site can't be "
                    "reached' page on http://localhost — that's expected. Copy "
                    "the entire URL from your browser's address bar and send "
                    "it back to me, then ask your question again."
                ),
                "authorize_url": e.authorize_url,
            }
            return [payload] if list_returning else payload
    return wrapper


# ─── access-token cache ────────────────────────────────────────────────────

_token_lock = threading.Lock()
_access_tokens: dict[str, tuple[str, float]] = {}


def _refresh_access_token(sender_id: str) -> str:
    """Exchange this sender's refresh_token for a fresh access_token.

    Tanda rotates the refresh_token on every call AND treats the old
    one as single-use, so we persist the new one immediately."""
    _require_creds()
    rt = _resolve_refresh_token(sender_id)
    r = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": rt,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "grant_type": "refresh_token",
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
            raise AuthRequired(sender_id) from None
        raise RuntimeError(f"Tanda token refresh failed ({r.status_code}): {body}")
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
        "Authorization": f"bearer {_access_token(sender_id)}",
        "Accept": "application/json",
    }


def _get(path: str, sender_id: str, params: dict[str, Any] | None = None) -> Any:
    """GET against the Tanda v2 API on behalf of `sender_id`."""
    _require_creds()
    if not path.startswith("/"):
        path = "/" + path
    url = f"{API_BASE}{path}"
    r = requests.get(url, headers=_headers(sender_id), params=params, timeout=30)
    if r.status_code == 401:
        # Stale cached access token — invalidate this sender's cache and
        # retry once before giving up.
        with _token_lock:
            _access_tokens.pop(sender_id, None)
        r = requests.get(url, headers=_headers(sender_id), params=params, timeout=30)
    if not r.ok:
        body = r.text[:500] if r.text else ""
        raise RuntimeError(f"Tanda GET {path} failed ({r.status_code}): {body}")
    if not r.content:
        return None
    return r.json()


def _resolve_sender(sender_id: str) -> str:
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
    "tanda",
    instructions=(
        "Tanda workforce-management access (read-only). Per-user authentication: "
        "every tool call must include `sender_id`, the chat_id from the "
        "Telegram channel tag of the message that prompted the request "
        "(<channel source=\"telegram\" chat_id=\"...\" ...>). Each user "
        "authorizes their own Tanda account once; subsequent calls return "
        "only data scoped to their Tanda account (their own shifts, their "
        "own leave; managers see their team).\n\n"
        "If a tool returns {\"auth_required\": true, ...}, relay the "
        "`message` field to the user via Telegram and stop. The user will "
        "click the link, sign in, hit a 'site can't be reached' page on "
        "localhost, and message the URL from their address bar back. When "
        "they do, call tanda_complete_auth(redirect_url, sender_id), then "
        "retry the original request."
    ),
)


# ─── auth-dance tools ──────────────────────────────────────────────────────

@mcp.tool()
def tanda_complete_auth(redirect_url: str, sender_id: str) -> dict:
    """Second leg of the per-user OAuth dance — call after the user pastes
    back the URL their browser ended on after signing in to Tanda.

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
        return {"error": "Tanda did not return a refresh_token — make sure your registered app is set up for the Authorization Code flow."}
    _save_user_token(sender_id, refresh_token)
    return {
        "linked": True,
        "message": "Authorized. You can now ask Tanda questions.",
    }


@mcp.tool()
def tanda_who_am_i(sender_id: str) -> dict:
    """Diagnostic: report whether the calling sender has linked their
    Tanda account, and (if so) when. Helpful for users who can't tell
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


# ─── identity ──────────────────────────────────────────────────────────────

@mcp.tool()
@requires_auth
def tanda_me(sender_id: str) -> dict:
    """Return the linked Tanda user's identity — name, email, user_id.

    Useful as a connectivity sanity check, and as the source of truth
    for `tanda_my_leave_balance` (which needs the user_id)."""
    return _get("/users/me", _resolve_sender(sender_id))


# ─── employees ─────────────────────────────────────────────────────────────

def _user_record_matches(u: dict, needle: str) -> bool:
    ndl = needle.lower()
    candidates = [
        u.get("name"),
        u.get("first_name"),
        u.get("last_name"),
        u.get("email"),
        u.get("preferred_name"),
    ]
    full = " ".join(c for c in [u.get("first_name"), u.get("last_name")] if c)
    candidates.append(full)
    return any(c and ndl in c.lower() for c in candidates)


def _summarize_user(u: dict) -> dict:
    return {
        "id": u.get("id") or u.get("user_id"),
        "name": u.get("name") or " ".join(c for c in [u.get("first_name"), u.get("last_name")] if c).strip() or None,
        "first_name": u.get("first_name"),
        "last_name": u.get("last_name"),
        "preferred_name": u.get("preferred_name"),
        "email": u.get("email"),
        "active": u.get("active"),
        "department_ids": u.get("department_ids"),
    }


@mcp.tool()
@requires_auth
def tanda_user_lookup(query: str, sender_id: str) -> list[dict]:
    """Find employees whose name or email contains `query` (case-insensitive
    substring). Returns Tanda user_ids for drill-down into rosters,
    timesheets, and leave.

    Args:
        query: Name fragment or email fragment. Matches first_name,
            last_name, full name, preferred_name, or email.
    """
    sender = _resolve_sender(sender_id)
    if not query or not query.strip():
        raise ValueError("query is required")
    needle = query.strip()
    data = _get("/users", sender)
    items = data if isinstance(data, list) else (data.get("users") or data.get("data") or [])
    if not isinstance(items, list):
        return []
    return [_summarize_user(u) for u in items if isinstance(u, dict) and _user_record_matches(u, needle)]


# ─── rosters / schedules ───────────────────────────────────────────────────

def _user_ids_param(user_ids_csv: str | None) -> dict[str, str]:
    if not user_ids_csv:
        return {}
    cleaned = ",".join(s.strip() for s in user_ids_csv.split(",") if s.strip())
    return {"user_ids": cleaned} if cleaned else {}


@mcp.tool()
@requires_auth
def tanda_roster(
    start_date: str,
    end_date: str,
    user_ids: str | None = None,
    show_costs: bool = False,
    *,
    sender_id: str,
) -> Any:
    """List rostered shifts (the schedule) between two dates.

    Args:
        start_date: ISO date 'YYYY-MM-DD' (inclusive).
        end_date: ISO date 'YYYY-MM-DD' (inclusive).
        user_ids: Optional comma-separated Tanda user_ids to restrict
            to. Omit to fetch everyone the caller is authorized to see.
        show_costs: If true, ask Tanda to compute the dollar cost of each
            shift (award-correct, includes overtime/penalty rates) and
            return it as a `cost` field. Requires the `cost` OAuth scope —
            users who linked before this scope was added will get an
            empty/missing cost field; have them re-run the sign-in dance.
    """
    sender = _resolve_sender(sender_id)
    if not (start_date and end_date):
        raise ValueError("start_date and end_date are required (YYYY-MM-DD).")
    params: dict[str, Any] = {"from": start_date, "to": end_date}
    params.update(_user_ids_param(user_ids))
    if show_costs:
        params["show_costs"] = "true"
    return _get("/schedules", sender, params=params)


# ─── timesheets ────────────────────────────────────────────────────────────

@mcp.tool()
@requires_auth
def tanda_timesheets(date: str, sender_id: str, show_costs: bool = False) -> Any:
    """Timesheet entries (clock-ins, breaks, shifts worked) for one date.

    Args:
        date: ISO date 'YYYY-MM-DD'. The endpoint returns one day at a
            time — call it multiple times for a date range.
        show_costs: If true, ask Tanda to compute the dollar cost of each
            timesheet (award-correct, includes overtime/penalty rates)
            and return it as a `cost` field. Requires the `cost` OAuth
            scope — users who linked before this scope was added need to
            re-run the sign-in dance to get cost-bearing tokens.
    """
    sender = _resolve_sender(sender_id)
    if not date:
        raise ValueError("date is required (YYYY-MM-DD).")
    params: dict[str, Any] = {}
    if show_costs:
        params["show_costs"] = "true"
    return _get(f"/timesheets/on/{date}", sender, params=params or None)


# ─── labour cost roll-up ───────────────────────────────────────────────────

# /shifts is the right endpoint for cost questions, NOT /timesheets/on/{date}.
# Confirmed empirically: /timesheets/on/{date}?show_costs=true is gated by
# a Tanda user-role permission ("View staff costs") that returns 403 with
# body "You do not have access to staff costs!" for accounts that lack it,
# even when the OAuth token has the `cost` scope. /shifts?show_costs=true
# does NOT trip the same gate.
#
# Shift schema (the fields we use, validated against a live POC):
#   id            int
#   user_id       int          (top-level — there is no nested `user` object)
#   start         int          unix seconds
#   finish        int          unix seconds  (NOT `end`)
#   break_length  int          minutes (sum of all breaks)
#   breaks        list[{length: minutes, paid: bool}]
#   cost          float|null   dollars; null when unapproved/not yet costed
#   status        str          APPROVED / DRAFT / ...
#
# Hours worked = (finish - start) / 3600  -  sum(breaks where !paid).length / 60
# Only unpaid breaks subtract — paid breaks count as worked time.
#
# /shifts returns a flat JSON array. The POC fetched ~300 shifts/week
# without any pagination params; we replicate that. If Tanda ever silently
# caps the result, we surface a heuristic warning when `len == 100` (the
# typical paginated default) so the operator knows to investigate.


def _shift_hours(s: dict) -> float | None:
    """Hours actually worked on this shift, per Tanda's documented schema."""
    start = s.get("start")
    finish = s.get("finish") or s.get("end")  # /shifts uses 'finish'
    if not (isinstance(start, (int, float)) and isinstance(finish, (int, float))):
        return None
    if finish <= start:
        return None
    span_hours = (float(finish) - float(start)) / 3600.0

    unpaid_minutes = 0.0
    breaks = s.get("breaks")
    if isinstance(breaks, list):
        for b in breaks:
            if not isinstance(b, dict) or b.get("paid"):
                continue
            length = b.get("length")
            if isinstance(length, (int, float)) and length > 0:
                unpaid_minutes += float(length)
    elif isinstance(s.get("break_length"), (int, float)) and s["break_length"] > 0:
        # Fallback for shifts where the breaks[] array isn't present —
        # break_length is the sum in minutes. We can't tell paid from
        # unpaid here, so treat the whole sum as unpaid (the conservative
        # choice for a labour-cost roll-up: undercount hours, never over).
        unpaid_minutes = float(s["break_length"])

    return max(0.0, span_hours - unpaid_minutes / 60.0)


def _user_name_map(sender_id: str) -> dict[Any, str]:
    """Build a {user_id: display_name} map by hitting /users once.

    /shifts only returns user_id, not names — joining is the caller's job.
    Falls back gracefully on permission errors so a user who can't list
    /users still gets cost numbers (just with raw IDs).
    """
    try:
        users = _get("/users", sender_id)
    except Exception:
        return {}
    if not isinstance(users, list):
        return {}
    out: dict[Any, str] = {}
    for u in users:
        if not isinstance(u, dict):
            continue
        uid = u.get("id")
        if uid is None:
            continue
        name = u.get("name") or " ".join(
            c for c in [u.get("legal_first_name"), u.get("legal_last_name")] if c
        ).strip()
        if name:
            out[uid] = name
    return out


@mcp.tool()
@requires_auth
def tanda_labour_cost(
    start_date: str,
    end_date: str,
    user_ids: str | None = None,
    *,
    sender_id: str,
) -> dict:
    """Per-user labour cost over a date range — answers "what did this
    period cost in wages?".

    Hits /shifts?from=&to=&show_costs=true once and aggregates Tanda's
    award-correct dollar cost (overtime and penalty rates included).
    Joins user_id → name via /users so the response is human-readable.

    Args:
        start_date: ISO date 'YYYY-MM-DD' (inclusive).
        end_date: ISO date 'YYYY-MM-DD' (inclusive).
        user_ids: Optional comma-separated Tanda user_ids to restrict to.

    Requires the `cost` OAuth scope (tick `cost` on the registered Tanda
    app + each user re-runs the sign-in dance once). Shifts that come
    back with cost=null — typically because they're unapproved or not
    yet costed — are tallied separately in `warnings[]` so the grand
    total reflects only the actually-costed work.
    """
    sender = _resolve_sender(sender_id)
    if not (start_date and end_date):
        raise ValueError("start_date and end_date are required (YYYY-MM-DD).")
    user_filter = set(
        s.strip() for s in (user_ids or "").split(",") if s.strip()
    ) or None

    name_by_uid = _user_name_map(sender)

    params: dict[str, Any] = {
        "from": start_date,
        "to": end_date,
        "show_costs": "true",
    }
    if user_filter:
        params["user_ids"] = ",".join(sorted(user_filter))

    payload = _get("/shifts", sender, params=params)
    if isinstance(payload, dict):
        # Defensive: in case a future Tanda revision wraps the array.
        for key in ("shifts", "data"):
            v = payload.get(key)
            if isinstance(v, list):
                payload = v
                break
        else:
            payload = []
    if not isinstance(payload, list):
        payload = []

    by_user: dict[Any, dict[str, Any]] = {}
    total_hours = 0.0
    total_cost = 0.0
    total_shifts = 0
    uncosted_shifts = 0

    for s in payload:
        if not isinstance(s, dict):
            continue
        uid = s.get("user_id")
        if uid is None:
            continue
        if user_filter and str(uid) not in user_filter:
            continue
        hours = _shift_hours(s)
        cost = s.get("cost") if isinstance(s.get("cost"), (int, float)) else None
        if cost is None:
            uncosted_shifts += 1
        row = by_user.setdefault(uid, {
            "user_id": uid,
            "name": name_by_uid.get(uid) or str(uid),
            "hours_worked": 0.0,
            "cost": 0.0,
            "shift_count": 0,
        })
        if hours is not None:
            row["hours_worked"] += hours
            total_hours += hours
        if cost is not None:
            row["cost"] += cost
            total_cost += cost
        row["shift_count"] += 1
        total_shifts += 1

    rows = sorted(by_user.values(), key=lambda r: r.get("cost") or 0.0, reverse=True)
    for r in rows:
        r["hours_worked"] = round(r["hours_worked"], 2)
        r["cost"] = round(r["cost"], 2)

    warnings: list[str] = []
    if uncosted_shifts:
        warnings.append(
            f"{uncosted_shifts} of {total_shifts} shifts had no cost field — "
            "usually means they're unapproved or haven't been costed yet. "
            "Total reflects only costed shifts."
        )
    if total_shifts == 100:
        # Heuristic: a flat 100 in the result smells like a paginated cap.
        # Tanda's /shifts has been observed to return everything in range
        # without limit/offset, but if the count happens to be exactly the
        # default page size, surface a hint.
        warnings.append(
            "Got exactly 100 shifts back — this may be a paginated cap "
            "rather than the true total. Cross-check by narrowing the "
            "date range and seeing if the count drops proportionally."
        )

    return {
        "start_date": start_date,
        "end_date": end_date,
        "by_user": rows,
        "totals": {
            "hours_worked": round(total_hours, 2),
            "cost": round(total_cost, 2),
            "user_count": len(rows),
            "shift_count": total_shifts,
        },
        "warnings": warnings,
    }


# ─── leave ─────────────────────────────────────────────────────────────────

@mcp.tool()
@requires_auth
def tanda_leave_requests(
    start_date: str,
    end_date: str,
    user_ids: str | None = None,
    *,
    sender_id: str,
) -> Any:
    """Leave requests that overlap the given date range.

    Args:
        start_date: ISO date 'YYYY-MM-DD' (inclusive).
        end_date: ISO date 'YYYY-MM-DD' (inclusive).
        user_ids: Optional comma-separated Tanda user_ids to restrict
            to. Omit to fetch everyone the caller is authorized to see.
    """
    sender = _resolve_sender(sender_id)
    if not (start_date and end_date):
        raise ValueError("start_date and end_date are required (YYYY-MM-DD).")
    params: dict[str, Any] = {"from": start_date, "to": end_date}
    params.update(_user_ids_param(user_ids))
    return _get("/leave", sender, params=params)


@mcp.tool()
@requires_auth
def tanda_leave_balance(user_id: str, sender_id: str) -> Any:
    """Leave balances for one user by Tanda user_id.

    Returns each leave type (annual leave, personal/sick, long-service,
    rostered day off, etc.) with the current balance in hours.
    """
    if not user_id:
        raise ValueError("user_id is required (resolve via tanda_user_lookup).")
    return _get(f"/leave_balances/user/{user_id}", _resolve_sender(sender_id))


@mcp.tool()
@requires_auth
def tanda_my_leave_balance(sender_id: str) -> Any:
    """Convenience: return leave balances for the authenticated Tanda user
    (the calling Telegram sender). Resolves /users/me → user_id, then
    /leave_balances/user/{user_id}.

    Use this for first-person questions like "how much annual leave do
    I have left?" — no need for the user to know their own user_id.
    """
    sender = _resolve_sender(sender_id)
    me = _get("/users/me", sender)
    if not isinstance(me, dict):
        return {"error": "Could not resolve linked Tanda user (unexpected /users/me shape).", "raw": me}
    uid = me.get("id") or me.get("user_id")
    if not uid:
        return {"error": "Could not extract user_id from /users/me.", "raw": me}
    return _get(f"/leave_balances/user/{uid}", sender)


# ─── escape hatch ──────────────────────────────────────────────────────────

@mcp.tool()
@requires_auth
def tanda_raw_get(
    path: str,
    params_json: str | None = None,
    *,
    sender_id: str,
) -> Any:
    """GET an arbitrary Tanda v2 API path.

    Use only for endpoints not covered by the typed tools above. `path`
    is relative to https://my.tanda.co/api/v2.
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
