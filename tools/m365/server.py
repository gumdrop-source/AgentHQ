"""Microsoft 365 MCP server — email, calendar, and OneDrive access via Graph API.

Auth is delegated (signs in as a user). Credentials come from the
systemd-creds vault under /etc/agents/credentials/, decrypted by systemd
into $CREDENTIALS_DIRECTORY when running as a service.

The MCP tools defined here mirror tool.json's `tools` map. Granting any
of them to an agent happens in the per-agent Permissions matrix.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

import msal
import requests
from mcp.server.fastmcp import FastMCP

# ─── credential helpers ─────────────────────────────────────────────────────

CRED_DIR = Path("/etc/agents/credentials")


def _decrypt_cred(name: str) -> str | None:
    """Get a credential value.

    Inside a systemd service with `LoadCredentialEncrypted=`, the decrypted
    plaintext sits at $CREDENTIALS_DIRECTORY/<name>. Outside (interactive
    debugging), shell out to `systemd-creds decrypt`.
    """
    runtime_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if runtime_dir:
        path = Path(runtime_dir) / name
        if path.exists():
            return path.read_text().strip()
    cred_path = CRED_DIR / f"{name}.cred"
    if cred_path.exists():
        try:
            r = subprocess.run(
                ["systemd-creds", "decrypt", str(cred_path), "-", f"--name={name}"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if r.returncode == 0 and r.stdout:
                return r.stdout.strip()
        except FileNotFoundError:
            pass
    return None


TENANT_ID = _decrypt_cred("m365_tenant_id") or os.environ.get("M365_TENANT_ID", "")
CLIENT_ID = _decrypt_cred("m365_client_id") or os.environ.get("M365_CLIENT_ID", "")

# Token cache lives next to the user's home so refresh tokens persist
# across service restarts. Per-agent: each agent that has m365 enabled
# gets its own cache file in its own home directory.
HOME = Path(os.environ.get("HOME", "/tmp"))
TOKEN_CACHE_FILE = HOME / ".m365_token_cache.json"

SCOPES = [
    "Mail.ReadWrite",
    "Mail.Send",
    "Files.ReadWrite.All",
    "Calendars.ReadWrite",
    "User.Read",
]
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# ─── auth ───────────────────────────────────────────────────────────────────

_cache = msal.SerializableTokenCache()
if TOKEN_CACHE_FILE.exists():
    _cache.deserialize(TOKEN_CACHE_FILE.read_text())

_app = msal.PublicClientApplication(
    CLIENT_ID,
    authority=f"https://login.microsoftonline.com/{TENANT_ID}",
    token_cache=_cache,
)


def _save_cache() -> None:
    if _cache.has_state_changed:
        TOKEN_CACHE_FILE.write_text(_cache.serialize())
        try:
            TOKEN_CACHE_FILE.chmod(0o600)
        except OSError:
            pass


PENDING_FLOW_FILE = HOME / ".m365_pending_flow.json"


class AuthRequired(Exception):
    """Raised by _token() when the user needs to complete a device-flow sign-in.

    Tools should catch this (or use @requires_auth) and return a structured
    response containing the URL + user code. The agent (LLM) sees the
    response and relays it to the human user via Telegram. The user signs
    in in their browser, comes back, asks again — _token() then completes
    the pending flow on the second invocation.
    """

    def __init__(self, verification_uri: str, user_code: str) -> None:
        self.verification_uri = verification_uri
        self.user_code = user_code
        super().__init__(f"Sign-in required: open {verification_uri}, enter {user_code}")


def _try_complete_pending_flow() -> str | None:
    """If a device flow is pending, poll once. Return access_token on success,
    None if not yet authorized, raise AuthRequired if still waiting."""
    if not PENDING_FLOW_FILE.exists():
        return None
    try:
        flow = json.loads(PENDING_FLOW_FILE.read_text())
    except Exception:
        PENDING_FLOW_FILE.unlink(missing_ok=True)
        return None

    # One-shot poll: msal's acquire_token_by_device_flow blocks unless we
    # exit early. exit_condition is called between polls; returning True
    # bails out with whatever the latest result is.
    attempts = {"n": 0}
    def one_poll(_flow):
        attempts["n"] += 1
        return attempts["n"] >= 1

    try:
        result = _app.acquire_token_by_device_flow(flow, exit_condition=one_poll)
    except Exception:
        PENDING_FLOW_FILE.unlink(missing_ok=True)
        return None

    if "access_token" in result:
        _save_cache()
        PENDING_FLOW_FILE.unlink(missing_ok=True)
        return result["access_token"]

    # Still waiting on user. Re-surface the URL+code so the agent prompts again.
    raise AuthRequired(flow.get("verification_uri", ""), flow.get("user_code", ""))


def _token() -> str:
    # 1. Try silent refresh from cached account
    accounts = _app.get_accounts()
    if accounts:
        result = _app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _save_cache()
            return result["access_token"]

    # 2. If a device flow is pending, attempt to complete it
    completed = _try_complete_pending_flow()
    if completed:
        return completed

    # 3. No cached account, no pending flow — start a new device flow
    flow = _app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Failed to start device flow: {flow}")
    PENDING_FLOW_FILE.write_text(json.dumps(flow))
    raise AuthRequired(flow["verification_uri"], flow["user_code"])


def requires_auth(fn: Callable) -> Callable:
    """Decorator: catch AuthRequired and return a structured response that
    instructs the agent to relay the sign-in URL+code to the user."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AuthRequired as e:
            return {
                "auth_required": True,
                "message": (
                    f"To do that I need access to your Microsoft 365. "
                    f"Open {e.verification_uri} in a browser and enter the code {e.user_code}. "
                    f"Sign in with your Microsoft account, then ask me again."
                ),
                "verification_uri": e.verification_uri,
                "user_code": e.user_code,
            }
    return wrapper


def _graph_get(path: str, params: dict[str, Any] | None = None) -> dict:
    r = requests.get(
        f"{GRAPH_BASE}{path}",
        headers={"Authorization": f"Bearer {_token()}"},
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _graph_post(path: str, body: dict) -> dict:
    r = requests.post(
        f"{GRAPH_BASE}{path}",
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=30,
    )
    r.raise_for_status()
    return r.json() if r.content else {}


# ─── MCP server ─────────────────────────────────────────────────────────────

mcp = FastMCP(
    "m365",
    instructions=(
        "Microsoft 365 access via Microsoft Graph. Read/search/draft/archive "
        "email, manage calendar events, search OneDrive. Each tool is granted "
        "individually per agent — the agent only sees the tools its operator "
        "explicitly enabled in the Permissions tab."
    ),
)


@mcp.tool()
@requires_auth
def outlook_email_search(
    query: str = "",
    folder: str = "inbox",
    unread_only: bool = False,
    limit: int = 20,
) -> list[dict]:
    """Search the mailbox.

    Args:
        query: Free-text search across subject, body, from. Empty = list recent.
        folder: 'inbox', 'sentitems', 'drafts', 'archive', or any folder display name.
        unread_only: If true, restrict to unread messages.
        limit: Max messages to return (1–50, default 20).
    """
    params: dict[str, Any] = {"$top": min(max(limit, 1), 50)}
    if query:
        params["$search"] = f'"{query}"'
    else:
        params["$orderby"] = "receivedDateTime desc"
    if unread_only:
        params["$filter"] = "isRead eq false"

    folder_path = "" if folder.lower() in ("", "inbox") else f"/mailFolders('{folder}')"
    data = _graph_get(f"/me{folder_path}/messages", params=params)

    return [
        {
            "id": m["id"],
            "subject": m.get("subject"),
            "from": m.get("from", {}).get("emailAddress", {}).get("address"),
            "received": m.get("receivedDateTime"),
            "is_read": m.get("isRead"),
            "preview": m.get("bodyPreview"),
        }
        for m in data.get("value", [])
    ]


@mcp.tool()
@requires_auth
def outlook_email_read(message_id: str) -> dict:
    """Read one email's full content by message ID."""
    m = _graph_get(f"/me/messages/{message_id}")
    return {
        "id": m["id"],
        "subject": m.get("subject"),
        "from": m.get("from", {}).get("emailAddress", {}).get("address"),
        "to": [r["emailAddress"]["address"] for r in m.get("toRecipients", [])],
        "cc": [r["emailAddress"]["address"] for r in m.get("ccRecipients", [])],
        "received": m.get("receivedDateTime"),
        "body": m.get("body", {}).get("content"),
        "body_type": m.get("body", {}).get("contentType"),
        "has_attachments": m.get("hasAttachments"),
    }


@mcp.tool()
@requires_auth
def outlook_email_draft(to: list[str], subject: str, body: str, cc: list[str] | None = None) -> dict:
    """Create a draft email. Does NOT send. Returns the draft's ID."""
    payload = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [{"emailAddress": {"address": a}} for a in (cc or [])],
    }
    draft = _graph_post("/me/messages", payload)
    return {"id": draft["id"], "web_link": draft.get("webLink")}


@mcp.tool()
@requires_auth
def outlook_email_archive(message_id: str) -> dict:
    """Move a message from Inbox to Archive."""
    return _graph_post(f"/me/messages/{message_id}/move", {"destinationId": "archive"})


@mcp.tool()
@requires_auth
def outlook_email_send(message_id: str | None = None, to: list[str] | None = None,
                       subject: str | None = None, body: str | None = None) -> dict:
    """Send an email — either an existing draft (by ID) or a new one inline.

    DESTRUCTIVE — sending cannot be undone. Confirm with the user first.
    """
    if message_id:
        # Send a previously-created draft
        return _graph_post(f"/me/messages/{message_id}/send", {})
    if not (to and subject and body):
        raise ValueError("Either message_id, or to+subject+body, must be provided.")
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        },
        "saveToSentItems": True,
    }
    return _graph_post("/me/sendMail", payload)


# ─── stubs for the rest ─────────────────────────────────────────────────────
# These are real MCP tool registrations — claude can call them — but they
# return a 'not implemented' message so they're discoverable in the
# Permissions matrix without yet being functional. Filled in next.

@mcp.tool()
def outlook_email_delete(message_id: str) -> str:
    """Permanently delete an email. DESTRUCTIVE."""
    return "outlook_email_delete: TODO — implementation pending"


@mcp.tool()
def calendar_search(start_iso: str, end_iso: str, calendar: str = "primary") -> str:
    """List events between two ISO timestamps."""
    return "calendar_search: TODO — implementation pending"


@mcp.tool()
def calendar_create_event(subject: str, start_iso: str, end_iso: str,
                           attendees: list[str] | None = None,
                           body: str | None = None,
                           location: str | None = None) -> str:
    """Create a calendar event."""
    return "calendar_create_event: TODO — implementation pending"


@mcp.tool()
def calendar_update_event(event_id: str, **kwargs) -> str:
    """Update an existing calendar event."""
    return "calendar_update_event: TODO — implementation pending"


@mcp.tool()
def calendar_delete_event(event_id: str) -> str:
    """Cancel and delete a calendar event. DESTRUCTIVE."""
    return "calendar_delete_event: TODO — implementation pending"


@mcp.tool()
def onedrive_search(query: str, limit: int = 20) -> str:
    """Search OneDrive files by name or content."""
    return "onedrive_search: TODO — implementation pending"


@mcp.tool()
def onedrive_read(file_id: str) -> str:
    """Download and read a OneDrive file's contents."""
    return "onedrive_read: TODO — implementation pending"


# ─── entry ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
