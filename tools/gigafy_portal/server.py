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
import os
import threading
import time
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


# ─── entry ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
