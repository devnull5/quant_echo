"""Create/refresh the Schwab OAuth token file (only needed for the Schwab provider).

    python -m quantecho.auth

Opens a browser to log in to Schwab and writes the token to SCHWAB_TOKEN_PATH
(default: tokens.json, which is gitignored). Refresh tokens expire after 7 days;
re-run this when the app reports the token is unusable.
"""
from __future__ import annotations

import sys

from .config import load_settings


def main() -> int:
    c = load_settings().creds
    key, secret = c.get("schwab_app_key", ""), c.get("schwab_app_secret", "")
    callback = c.get("schwab_callback") or "https://127.0.0.1:8182"
    token_path = c.get("schwab_token_path") or "tokens.json"
    if not key or not secret:
        print("Set SCHWAB_APP_KEY and SCHWAB_APP_SECRET in .env (see .env.example) or save them in the app.")
        return 1
    from schwab.auth import client_from_login_flow

    print(f"Logging in — callback {callback}, token -> {token_path}")
    client = client_from_login_flow(api_key=key, app_secret=secret, callback_url=callback, token_path=token_path)
    r = client.get_quotes(["SPY"])
    print(f"Token OK. Test quote request: HTTP {r.status_code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
