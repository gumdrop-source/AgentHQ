"""One-time device-flow sign-in to populate the m365 token cache.

Run interactively as the user that the agent will run as:

    python /opt/agents/tools/m365/auth.py

It prompts you to open a URL, paste a code, and sign in to your
Microsoft account. The resulting refresh token is cached at
~/.m365_token_cache.json so subsequent server runs auto-renew silently.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import msal

# Re-use the same credential resolution as server.py
sys.path.insert(0, str(Path(__file__).parent))
from server import TENANT_ID, CLIENT_ID, SCOPES, TOKEN_CACHE_FILE  # noqa: E402


def main() -> int:
    if not (TENANT_ID and CLIENT_ID):
        print("ERROR: m365_tenant_id / m365_client_id not in vault.", file=sys.stderr)
        print("Activate the m365 integration in the Integrations tab first.", file=sys.stderr)
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
        print(f"Failed to start device flow: {flow}", file=sys.stderr)
        return 2

    print()
    print(flow["message"])
    print()
    print("Waiting for sign-in to complete...")

    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        print(f"Auth failed: {result.get('error_description', result)}", file=sys.stderr)
        return 3

    TOKEN_CACHE_FILE.write_text(cache.serialize())
    try:
        TOKEN_CACHE_FILE.chmod(0o600)
    except OSError:
        pass

    upn = result.get("id_token_claims", {}).get("preferred_username", "unknown")
    print(f"\nSigned in as: {upn}")
    print(f"Token cache: {TOKEN_CACHE_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
