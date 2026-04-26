"""One-time device-flow sign-in to populate the m365 token cache.

Two modes:

  python auth.py              interactive — prints human-readable lines,
                              suitable for running in a terminal yourself

  python auth.py --json-flow  machine-readable JSON events on stdout, one
                              per line, used by agent-control-web to drive
                              the UI sign-in flow

Either way the resulting refresh token is cached at
~/.m365_token_cache.json so subsequent server runs auto-renew silently.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import msal

# Re-use the same credential resolution as server.py
sys.path.insert(0, str(Path(__file__).parent))
from server import TENANT_ID, CLIENT_ID, SCOPES, TOKEN_CACHE_FILE  # noqa: E402


def emit(json_mode: bool, **event):
    if json_mode:
        print(json.dumps(event), flush=True)
    elif event.get("event") == "flow_started":
        print(f"\nOpen {event['verification_uri']}\nEnter code: {event['user_code']}\n", flush=True)
    elif event.get("event") == "success":
        print(f"\nSigned in as: {event.get('user')}", flush=True)
        print(f"Token cache: {event.get('cache')}", flush=True)
    elif event.get("event") == "error":
        print(f"Auth failed: {event.get('error')}", file=sys.stderr, flush=True)


def main(json_mode: bool = False) -> int:
    if not (TENANT_ID and CLIENT_ID):
        emit(json_mode, event="error", error="m365_tenant_id / m365_client_id not in vault — activate the m365 integration first")
        return 1

    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE_FILE.exists():
        cache.deserialize(TOKEN_CACHE_FILE.read_text())

    app = msal.PublicClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        token_cache=cache,
    )

    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        emit(json_mode, event="error", error=f"Failed to start device flow: {flow}")
        return 2

    emit(
        json_mode,
        event="flow_started",
        verification_uri=flow.get("verification_uri", ""),
        user_code=flow.get("user_code", ""),
        expires_in=flow.get("expires_in", 0),
    )

    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        emit(json_mode, event="error", error=result.get("error_description", "auth failed"))
        return 3

    TOKEN_CACHE_FILE.write_text(cache.serialize())
    try:
        TOKEN_CACHE_FILE.chmod(0o600)
    except OSError:
        pass

    upn = result.get("id_token_claims", {}).get("preferred_username", "unknown")
    emit(json_mode, event="success", user=upn, cache=str(TOKEN_CACHE_FILE))
    return 0


if __name__ == "__main__":
    json_mode = "--json-flow" in sys.argv
    sys.exit(main(json_mode))
