"""Create/refresh the Schwab OAuth token file.

    python -m quantecho.auth

Opens a browser to log in to Schwab and writes the token to SCHWAB_TOKEN_PATH
(default: tokens.json, which is gitignored). Refresh tokens expire after 7 days;
re-run this when the GUI reports the token is unusable.
"""
from __future__ import annotations

import sys

from .config import load_settings


def main() -> int:
    s = load_settings()
    if not s.schwab_app_key or not s.schwab_app_secret:
        print("Set SCHWAB_APP_KEY and SCHWAB_APP_SECRET in .env (see .env.example) or save them in the GUI.")
        return 1
    from schwab.auth import client_from_login_flow

    print(f"Logging in — callback {s.schwab_callback}, token -> {s.schwab_token_path}")
    client = client_from_login_flow(
        api_key=s.schwab_app_key,
        app_secret=s.schwab_app_secret,
        callback_url=s.schwab_callback,
        token_path=s.schwab_token_path,
    )
    r = client.get_quotes(["SPY"])
    print(f"Token OK. Test quote request: HTTP {r.status_code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
