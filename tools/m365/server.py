"""Microsoft 365 MCP server — email, calendar, and OneDrive access via Graph API.

Auth is delegated (signs in as a user). Credentials come from the
systemd-creds vault under /etc/agents/credentials/, decrypted by systemd
into $CREDENTIALS_DIRECTORY when running as a service.

The MCP tools defined here mirror tool.json's `tools` map. Granting any
of them to an agent happens in the per-agent Permissions matrix.
"""

from __future__ import annotations

import base64
import functools
import json
import mimetypes
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, get_origin, get_type_hints

import msal
import requests
from mcp.server.fastmcp import FastMCP

# ─── credential helpers ─────────────────────────────────────────────────────

CRED_DIR = Path("/etc/agents/credentials")


def _decrypt_cred(name: str) -> str | None:
    """Get a credential value.

    Inside a systemd service with `LoadCredentialEncrypted=`, the decrypted
    plaintext sits at $CREDENTIALS_DIRECTORY/<name>. Outside (interactive
    debugging as root), shell out to `systemd-creds decrypt`.

    All filesystem ops are guarded so an inaccessible cred yields None
    (caller falls back to env vars) rather than crashing module import.
    Python 3.12 changed Path.exists() to propagate PermissionError — and
    /etc/agents/credentials/*.cred is root:root 0600 — so the bare
    `cred_path.exists()` check used to crash the unprivileged service user
    at module-import time, which broke the MCP stdio handshake.
    """
    runtime_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if runtime_dir:
        try:
            return (Path(runtime_dir) / name).read_text().strip()
        except OSError:
            pass
    cred_path = CRED_DIR / f"{name}.cred"
    if not os.access(cred_path, os.R_OK):
        return None
    try:
        r = subprocess.run(
            ["systemd-creds", "decrypt", str(cred_path), "-", f"--name={name}"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if r.returncode == 0 and r.stdout:
            return r.stdout.strip()
    except (FileNotFoundError, OSError):
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

# Lazy: msal.PublicClientApplication validates the authority URL during
# construction and raises ValueError if TENANT_ID is empty. Building it
# eagerly at module import would crash any spawn that doesn't have creds
# in environ — and the crash kills the MCP handshake before tool listing.
# Defer construction so the server can register tools and only fail (with
# a clear error) when a tool is actually invoked without creds.
_app: msal.PublicClientApplication | None = None


def _get_app() -> msal.PublicClientApplication:
    global _app
    if _app is None:
        if not (TENANT_ID and CLIENT_ID):
            raise RuntimeError(
                "Microsoft 365 credentials are not loaded. The operator must "
                "activate the m365 integration in agent-control (which provisions "
                "the encrypted credentials and reloads the agent service)."
            )
        _app = msal.PublicClientApplication(
            CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{TENANT_ID}",
            token_cache=_cache,
        )
    return _app


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
        result = _get_app().acquire_token_by_device_flow(flow, exit_condition=one_poll)
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
    app = _get_app()
    # 1. Try silent refresh from cached account
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _save_cache()
            return result["access_token"]

    # 2. If a device flow is pending, attempt to complete it
    completed = _try_complete_pending_flow()
    if completed:
        return completed

    # 3. No cached account, no pending flow — start a new device flow
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Failed to start device flow: {flow}")
    PENDING_FLOW_FILE.write_text(json.dumps(flow))
    raise AuthRequired(flow["verification_uri"], flow["user_code"])


def requires_auth(fn: Callable) -> Callable:
    """Decorator: catch AuthRequired and return a structured response that
    instructs the agent to relay the sign-in URL+code to the user.

    The auth payload is wrapped in a list when the wrapped tool's declared
    return type is list-shaped — without this, FastMCP's pydantic output
    validation rejects the dict against e.g. `list[dict]` annotations.
    """
    # Resolve annotations once at decoration time. `from __future__ import
    # annotations` keeps fn.__annotations__ as strings, so get_type_hints()
    # is required to materialize the actual types.
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
                    f"To do that I need access to your Microsoft 365. "
                    f"Open {e.verification_uri} in a browser and enter the code {e.user_code}. "
                    f"Sign in with your Microsoft account, then ask me again."
                ),
                "verification_uri": e.verification_uri,
                "user_code": e.user_code,
            }
            return [payload] if list_returning else payload
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


def _graph_patch(path: str, body: dict) -> dict:
    r = requests.patch(
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


def _graph_delete(path: str) -> None:
    r = requests.delete(
        f"{GRAPH_BASE}{path}",
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=30,
    )
    r.raise_for_status()


def _graph_get_bytes(path: str) -> tuple[bytes, str]:
    """GET a binary endpoint (e.g. file content). Returns (bytes, content_type)."""
    r = requests.get(
        f"{GRAPH_BASE}{path}",
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=60,
    )
    r.raise_for_status()
    return r.content, r.headers.get("Content-Type", "application/octet-stream")


# ─── attachments ────────────────────────────────────────────────────────────

# Graph caps inline attachment bytes at 3 MB per item; anything larger has to
# go through createUploadSession + chunked PUT. We split on this boundary
# transparently so the caller doesn't have to think about it.
ATTACHMENT_INLINE_MAX_BYTES = 3 * 1024 * 1024

# Chunk size for the resumable upload session. 5 MiB is well within Graph's
# stated 60 MiB ceiling, big enough to keep round-trips reasonable on a 10 MB
# xlsx, and aligned to 320 KiB (Graph requires multiples of 320 KiB except
# for the final chunk).
ATTACHMENT_CHUNK_BYTES = 5 * 1024 * 1024


def _resolve_attachment(att: dict) -> tuple[str, bytes, str]:
    """Materialize one attachment spec into (name, raw_bytes, content_type).

    Accepts either:
      - {"path": "/abs/path/file.xlsx", "name"?, "content_type"?}
      - {"name": "file.xlsx", "content_bytes": "<base64>", "content_type"?}

    `path` is preferred when the file is already on disk in the agent's
    sandbox — saves round-tripping bytes through the LLM. `content_bytes`
    is for files the LLM has materialized inline (e.g. generated CSV).
    """
    if not isinstance(att, dict):
        raise ValueError(f"attachment must be an object, got {type(att).__name__}")

    name = att.get("name")
    content_type = att.get("content_type") or att.get("contentType")

    if att.get("path"):
        p = Path(att["path"])
        if not p.is_file():
            raise ValueError(f"attachment path not a file: {att['path']}")
        raw = p.read_bytes()
        if not name:
            name = p.name
        if not content_type:
            content_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        return name, raw, content_type

    cb = att.get("content_bytes") or att.get("contentBytes")
    if cb:
        if not name:
            raise ValueError("attachment with content_bytes requires `name`.")
        try:
            raw = base64.b64decode(cb, validate=True)
        except Exception as e:
            raise ValueError(f"attachment content_bytes is not valid base64: {e}") from e
        if not content_type:
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return name, raw, content_type

    raise ValueError("attachment must have either `path` or `content_bytes`.")


def _attach_small(message_id: str, name: str, raw: bytes, content_type: str) -> dict:
    """Attach a <3 MB file as an inline fileAttachment payload."""
    return _graph_post(
        f"/me/messages/{message_id}/attachments",
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": name,
            "contentType": content_type,
            "contentBytes": base64.b64encode(raw).decode("ascii"),
        },
    )


def _attach_large(message_id: str, name: str, raw: bytes, content_type: str) -> dict:
    """Attach a >=3 MB file via Graph's resumable upload session.

    Graph requires chunked PUTs to the session URL with a Content-Range header.
    The session URL is pre-authorized — no Bearer header on the chunk PUTs.
    """
    session = _graph_post(
        f"/me/messages/{message_id}/attachments/createUploadSession",
        {
            "AttachmentItem": {
                "attachmentType": "file",
                "name": name,
                "size": len(raw),
                "contentType": content_type,
            }
        },
    )
    upload_url = session.get("uploadUrl")
    if not upload_url:
        raise RuntimeError(f"createUploadSession did not return uploadUrl: {session}")

    total = len(raw)
    offset = 0
    last: dict[str, Any] = {}
    while offset < total:
        end = min(offset + ATTACHMENT_CHUNK_BYTES, total) - 1
        chunk = raw[offset:end + 1]
        r = requests.put(
            upload_url,
            headers={
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end}/{total}",
            },
            data=chunk,
            timeout=120,
        )
        # 200/201 on the final chunk, 202 on intermediate chunks.
        if r.status_code not in (200, 201, 202):
            raise RuntimeError(
                f"upload chunk {offset}-{end}/{total} failed "
                f"({r.status_code}): {r.text[:300]}"
            )
        try:
            last = r.json() if r.content else {}
        except ValueError:
            last = {}
        offset = end + 1
    return last or {"uploaded": True, "name": name, "size": total}


def _attach_one(message_id: str, att: dict) -> dict:
    name, raw, content_type = _resolve_attachment(att)
    if len(raw) < ATTACHMENT_INLINE_MAX_BYTES:
        return _attach_small(message_id, name, raw, content_type)
    return _attach_large(message_id, name, raw, content_type)


def _parse_attachments_json(attachments_json: str | None) -> list[dict]:
    if not attachments_json:
        return []
    try:
        items = json.loads(attachments_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"attachments_json is not valid JSON: {e}") from e
    if not isinstance(items, list):
        raise ValueError("attachments_json must be a JSON array of attachment objects.")
    return items


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
def outlook_email_draft(
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    attachments_json: str | None = None,
) -> dict:
    """Create a draft email. Does NOT send. Returns the draft's ID.

    Args:
        to: Recipient email addresses.
        subject: Subject line.
        body: Plain-text body.
        cc: Optional CC recipients.
        attachments_json: Optional JSON array of attachment specs. Each spec
            is either {"path": "/abs/path/file.xlsx"} (preferred when the
            file is on the agent's filesystem) or {"name": "file.xlsx",
            "content_bytes": "<base64>"}. `content_type` is optional in
            both forms — falls back to mimetypes.guess_type or
            application/octet-stream. Files >=3 MB are uploaded via
            Graph's resumable upload session automatically.
    """
    attachments = _parse_attachments_json(attachments_json)
    payload = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [{"emailAddress": {"address": a}} for a in (cc or [])],
    }
    draft = _graph_post("/me/messages", payload)
    draft_id = draft["id"]
    attached_names: list[str] = []
    for att in attachments:
        result = _attach_one(draft_id, att)
        if isinstance(result, dict) and result.get("name"):
            attached_names.append(result["name"])
    return {
        "id": draft_id,
        "web_link": draft.get("webLink"),
        "attachments": attached_names,
    }


@mcp.tool()
@requires_auth
def outlook_email_archive(message_id: str) -> dict:
    """Move a message from Inbox to Archive."""
    return _graph_post(f"/me/messages/{message_id}/move", {"destinationId": "archive"})


@mcp.tool()
@requires_auth
def outlook_email_send(
    message_id: str | None = None,
    to: list[str] | None = None,
    subject: str | None = None,
    body: str | None = None,
    cc: list[str] | None = None,
    attachments_json: str | None = None,
) -> dict:
    """Send an email — either an existing draft (by ID) or a new one inline.

    DESTRUCTIVE — sending cannot be undone. Confirm with the user first.

    Args:
        message_id: Send an existing draft. If `attachments_json` is also
            given, the files are attached to the draft before sending.
        to, subject, body, cc: Inline-send fields. Required when
            `message_id` is not given.
        attachments_json: Optional JSON array of attachment specs. See
            outlook_email_draft for the shape. Files >=3 MB use the
            resumable upload session automatically. When attachments
            are present in the inline-send path, the email is composed
            as a draft and sent (one extra round-trip vs. plain
            sendMail), since /me/sendMail doesn't accept large
            attachments inline.
    """
    attachments = _parse_attachments_json(attachments_json)

    if message_id:
        # Existing draft. Attach any newly-supplied files, then send.
        for att in attachments:
            _attach_one(message_id, att)
        return _graph_post(f"/me/messages/{message_id}/send", {})

    if not (to and subject and body):
        raise ValueError("Either message_id, or to+subject+body, must be provided.")

    if attachments:
        # Compose-and-send path with attachments: draft → attach → send.
        # Reuses outlook_email_draft's logic so big files automatically use
        # the resumable upload session.
        draft = outlook_email_draft(
            to=to, subject=subject, body=body, cc=cc, attachments_json=attachments_json
        )
        # outlook_email_draft is decorated with @requires_auth, which can
        # return an auth_required dict instead of a draft id. Propagate that
        # back unchanged so the agent prompts the user to sign in.
        if isinstance(draft, dict) and draft.get("auth_required"):
            return draft
        _graph_post(f"/me/messages/{draft['id']}/send", {})
        return {"sent": True, "id": draft["id"], "attachments": draft.get("attachments", [])}

    # Plain inline send — fastest path, single round-trip.
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in (cc or [])],
        },
        "saveToSentItems": True,
    }
    return _graph_post("/me/sendMail", payload)


@mcp.tool()
@requires_auth
def outlook_email_delete(message_id: str) -> dict:
    """Permanently delete an email.

    DESTRUCTIVE — the message is moved to Deleted Items and then removed; it
    cannot be recovered through this tool. Confirm with the user before calling.
    Prefer outlook_email_archive for non-destructive cleanup.
    """
    _graph_delete(f"/me/messages/{message_id}")
    return {"deleted": True, "id": message_id}


@mcp.tool()
@requires_auth
def calendar_search(start_iso: str, end_iso: str, limit: int = 50) -> list[dict]:
    """List calendar events whose time range overlaps [start_iso, end_iso].

    Args:
        start_iso: Window start as ISO 8601 (e.g. '2026-04-28T00:00:00Z').
        end_iso: Window end as ISO 8601.
        limit: Max events to return (1–100, default 50).

    Uses Graph's calendarView, which expands recurring events into instances.
    """
    params: dict[str, Any] = {
        "startDateTime": start_iso,
        "endDateTime": end_iso,
        "$top": min(max(limit, 1), 100),
        "$orderby": "start/dateTime",
    }
    data = _graph_get("/me/calendarView", params=params)
    return [
        {
            "id": e["id"],
            "subject": e.get("subject"),
            "start": (e.get("start") or {}).get("dateTime"),
            "end": (e.get("end") or {}).get("dateTime"),
            "timezone": (e.get("start") or {}).get("timeZone"),
            "location": (e.get("location") or {}).get("displayName"),
            "organizer": ((e.get("organizer") or {}).get("emailAddress") or {}).get("address"),
            "attendees": [
                ((a.get("emailAddress") or {}).get("address"))
                for a in e.get("attendees", [])
            ],
            "is_online_meeting": e.get("isOnlineMeeting"),
            "web_link": e.get("webLink"),
        }
        for e in data.get("value", [])
    ]


@mcp.tool()
@requires_auth
def calendar_create_event(
    subject: str,
    start_iso: str,
    end_iso: str,
    timezone: str = "UTC",
    attendees: list[str] | None = None,
    body: str | None = None,
    location: str | None = None,
) -> dict:
    """Create a calendar event on the user's primary calendar.

    Args:
        subject: Event title.
        start_iso: Start time as ISO 8601 (interpreted in `timezone`).
        end_iso: End time as ISO 8601 (interpreted in `timezone`).
        timezone: IANA tz name like 'America/Los_Angeles', or 'UTC'.
        attendees: Email addresses to invite.
        body: Event description (plain text).
        location: Display name for the location.
    """
    payload: dict[str, Any] = {
        "subject": subject,
        "start": {"dateTime": start_iso, "timeZone": timezone},
        "end": {"dateTime": end_iso, "timeZone": timezone},
    }
    if body is not None:
        payload["body"] = {"contentType": "Text", "content": body}
    if location is not None:
        payload["location"] = {"displayName": location}
    if attendees:
        payload["attendees"] = [
            {"emailAddress": {"address": a}, "type": "required"} for a in attendees
        ]
    e = _graph_post("/me/events", payload)
    return {
        "id": e["id"],
        "subject": e.get("subject"),
        "web_link": e.get("webLink"),
        "start": (e.get("start") or {}).get("dateTime"),
        "end": (e.get("end") or {}).get("dateTime"),
    }


@mcp.tool()
@requires_auth
def calendar_update_event(
    event_id: str,
    subject: str | None = None,
    start_iso: str | None = None,
    end_iso: str | None = None,
    timezone: str | None = None,
    attendees: list[str] | None = None,
    body: str | None = None,
    location: str | None = None,
) -> dict:
    """Modify an existing calendar event. Only fields you pass are changed.

    To change start or end times, pass start_iso/end_iso (and timezone if it
    differs from the original). Passing `attendees` REPLACES the attendee list.
    """
    payload: dict[str, Any] = {}
    if subject is not None:
        payload["subject"] = subject
    # Graph requires the full {dateTime, timeZone} object to change either field.
    # Default to UTC if a timestamp is given without a timezone.
    if start_iso is not None:
        payload["start"] = {"dateTime": start_iso, "timeZone": timezone or "UTC"}
    if end_iso is not None:
        payload["end"] = {"dateTime": end_iso, "timeZone": timezone or "UTC"}
    if body is not None:
        payload["body"] = {"contentType": "Text", "content": body}
    if location is not None:
        payload["location"] = {"displayName": location}
    if attendees is not None:
        payload["attendees"] = [
            {"emailAddress": {"address": a}, "type": "required"} for a in attendees
        ]
    if not payload:
        raise ValueError("calendar_update_event: pass at least one field to change.")
    e = _graph_patch(f"/me/events/{event_id}", payload)
    return {
        "id": e["id"],
        "subject": e.get("subject"),
        "web_link": e.get("webLink"),
        "start": (e.get("start") or {}).get("dateTime"),
        "end": (e.get("end") or {}).get("dateTime"),
    }


@mcp.tool()
@requires_auth
def calendar_delete_event(event_id: str) -> dict:
    """Cancel and delete a calendar event.

    DESTRUCTIVE — the event is removed from the user's calendar and a
    cancellation is sent to attendees. Confirm with the user before calling.
    """
    _graph_delete(f"/me/events/{event_id}")
    return {"deleted": True, "id": event_id}


@mcp.tool()
@requires_auth
def onedrive_search(query: str, limit: int = 20) -> list[dict]:
    """Search OneDrive files by name or content.

    Args:
        query: Search terms — matches file names and indexed content.
        limit: Max items to return (1–50, default 20).
    """
    # Graph's search(q='...') endpoint URL-encodes via requests' params.
    # The single quotes around the query are part of the OData function call.
    safe = query.replace("'", "''")
    params = {"$top": min(max(limit, 1), 50)}
    data = _graph_get(f"/me/drive/root/search(q='{safe}')", params=params)
    return [
        {
            "id": item["id"],
            "name": item.get("name"),
            "size": item.get("size"),
            "is_folder": "folder" in item,
            "mime_type": (item.get("file") or {}).get("mimeType"),
            "modified": item.get("lastModifiedDateTime"),
            "web_url": item.get("webUrl"),
            "path": (item.get("parentReference") or {}).get("path"),
        }
        for item in data.get("value", [])
    ]


# 256 KB cap on inline file content. Larger files return metadata only with a
# note pointing the agent at the web_url — dumping multi-MB blobs into the
# LLM context is both expensive and rarely useful.
ONEDRIVE_READ_MAX_BYTES = 256 * 1024


@mcp.tool()
@requires_auth
def onedrive_read(file_id: str) -> dict:
    """Download and read a OneDrive file's contents.

    Returns text inline for text-shaped files (utf-8 decodable, ≤256 KB).
    For binary or oversize files, returns metadata + base64 (binary, ≤256 KB)
    or a size-only response telling the agent to fetch via web_url.
    """
    meta = _graph_get(f"/me/drive/items/{file_id}")
    if "folder" in meta:
        raise ValueError(f"onedrive_read: '{meta.get('name')}' is a folder, not a file.")

    size = meta.get("size") or 0
    name = meta.get("name")
    mime = (meta.get("file") or {}).get("mimeType") or "application/octet-stream"
    web_url = meta.get("webUrl")

    base = {
        "id": meta["id"],
        "name": name,
        "size": size,
        "mime_type": mime,
        "web_url": web_url,
    }

    if size > ONEDRIVE_READ_MAX_BYTES:
        return {
            **base,
            "content": None,
            "truncated": True,
            "message": (
                f"File is {size} bytes, larger than the {ONEDRIVE_READ_MAX_BYTES}-byte "
                "inline limit. Open it via web_url instead."
            ),
        }

    content, _ctype = _graph_get_bytes(f"/me/drive/items/{file_id}/content")
    try:
        text = content.decode("utf-8")
        return {**base, "encoding": "utf-8", "content": text}
    except UnicodeDecodeError:
        return {
            **base,
            "encoding": "base64",
            "content": base64.b64encode(content).decode("ascii"),
        }


# ─── entry ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
