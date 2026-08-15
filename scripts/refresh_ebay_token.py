"""Re-mint the eBay REST user token and persist it to ebay_rest.json.

Run this after changing the ``scopes`` list. Scopes are fixed when the user
consents, so editing the list alone changes nothing: while ``refresh_token``
and ``refresh_token_expiry`` are both set, ebay_rest sets
``_allow_get_user_consent = False`` and only ever *refreshes*, and a refresh
returns the scopes the token was originally granted. A newly added scope then
fails with HTTP 403 / errorId 1100 / domain ACCESS ("Insufficient permissions
to fulfill the request") even though the config looks right.

Clearing both fields is what re-enables consent — clearing only the token
raises error 96004, since a token without its expiry is rejected.

Why this script exists: ``ebay_rest`` obtains a refresh token but never writes
it back to the config file — it only lives in memory for the life of the
process. Without persisting it, every run reopens the consent browser. This
script does the reset / acquire / save cycle, backing the config up first.

Usage:
    python scripts/refresh_ebay_token.py --check    # report state, change nothing
    python scripts/refresh_ebay_token.py --reset    # blank both token fields
    python scripts/refresh_ebay_token.py            # consent, then save
    python scripts/refresh_ebay_token.py --verify   # also make a read-only Stores call

Consent opens a real Chromium window (Playwright, non-headless). It fills the
credentials from ebay_rest.json and pauses for 2FA if eBay asks. Requires the
"complete" variant:

    pip install "ebay_rest[complete]" && playwright install chromium

Note: this reads private attributes of ebay_rest's token objects, verified
against ebay_rest 1.1.4. Re-check it after upgrading that package.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REQUIRED_SCOPE_HINT = "https://api.ebay.com/oauth/api_scope/sell.stores"


def config_path() -> Path:
    """Resolve ebay_rest.json the same way the app does."""
    override = os.getenv("EBAY_REST_CONFIG_PATH")
    if override:
        return Path(override)

    from shoebox.settings import get_settings

    return Path(get_settings().ebay.path) / "ebay_rest.json"


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Config not found: {path}")
    return json.loads(path.read_text())


def report(config: dict[str, Any], user_key: str) -> None:
    user = config["users"][user_key]
    scopes = user.get("scopes") or []
    has_token = bool(user.get("refresh_token"))

    print(f"user: {user_key}")
    print(f"scopes ({len(scopes)}):")
    for s in scopes:
        print(f"  {s}")
    if REQUIRED_SCOPE_HINT not in scopes:
        print(f"  !! missing {REQUIRED_SCOPE_HINT} — store category calls will 403")
    print(f"refresh_token: {'present' if has_token else 'EMPTY (consent will run)'}")
    print(f"refresh_token_expiry: {user.get('refresh_token_expiry') or 'EMPTY'}")
    if has_token:
        print(
            "  note: while a refresh token is set, ebay_rest only refreshes and never\n"
            "  re-consents, so it keeps the scopes granted when it was minted.\n"
            "  Run with --reset to clear it and force a fresh grant."
        )


def backup(path: Path) -> Path:
    dest = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, dest)
    return dest


def reset(path: Path, config: dict[str, Any], user_key: str) -> None:
    """Blank both token fields so the next run re-runs the consent flow."""
    dest = backup(path)
    config["users"][user_key]["refresh_token"] = ""
    config["users"][user_key]["refresh_token_expiry"] = ""
    path.write_text(json.dumps(config, indent=2) + "\n")
    print(f"\nCleared refresh_token and refresh_token_expiry (backup at {dest.name}).")
    print("Re-run without --reset to consent and save a new token.")


def build_api(config_file: Path, user_key: str):
    """Construct an ebay_rest API bound to this config."""
    from ebay_rest import API

    from shoebox.settings import get_settings

    settings = get_settings()
    return API(
        application=settings.ebay.application,
        user=user_key,
        header=settings.ebay.header,
        path=str(config_file.parent),
    )


def extract_refresh_token(api: Any) -> tuple[str, str | None]:
    """Pull the freshly obtained refresh token out of ebay_rest's token object."""
    token_obj = getattr(api, "_user_token", None)
    if token_obj is None:
        raise SystemExit("Could not reach api._user_token — check the ebay_rest version.")

    # Force acquisition; this is what triggers the consent browser when needed.
    token_obj.get()

    oauth = getattr(token_obj, "_user_refresh_token", None)
    refresh = getattr(oauth, "refresh_token", None) if oauth is not None else None
    if not refresh:
        raise SystemExit("No refresh token was produced; consent may have been cancelled.")

    expiry = getattr(oauth, "refresh_token_expiry", None)
    if isinstance(expiry, datetime):
        expiry = expiry.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return refresh, expiry


def save(
    path: Path, config: dict[str, Any], user_key: str, refresh: str, expiry: str | None
) -> None:
    dest = backup(path)

    config["users"][user_key]["refresh_token"] = refresh
    config["users"][user_key]["refresh_token_expiry"] = expiry or ""
    path.write_text(json.dumps(config, indent=2) + "\n")

    print(f"Saved refresh token to {path} (backup at {dest.name})")
    print(f"refresh_token_expiry: {expiry or 'not reported'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default=None, help="users.<key> in ebay_rest.json")
    parser.add_argument("--check", action="store_true", help="Report state and exit")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Blank both token fields so the next run re-consents, then exit",
    )
    parser.add_argument(
        "--verify", action="store_true", help="After saving, make a read-only Stores call"
    )
    args = parser.parse_args()

    path = config_path()
    config = load_config(path)

    user_key = args.user
    if user_key is None:
        from shoebox.settings import get_settings

        user_key = get_settings().ebay.user
    if user_key not in config.get("users", {}):
        raise SystemExit(f"No users.{user_key} in {path}")

    report(config, user_key)
    if args.check:
        return 0

    if args.reset:
        reset(path, config, user_key)
        return 0

    if config["users"][user_key].get("refresh_token"):
        raise SystemExit(
            "\nA refresh token is already set, so ebay_rest will refresh rather than\n"
            "re-consent and the scopes cannot change. Run with --reset first."
        )

    print("\nOpening the eBay consent browser; complete any 2FA prompt...")
    api = build_api(path, user_key)
    refresh, expiry = extract_refresh_token(api)
    save(path, config, user_key, refresh, expiry)

    if args.verify:
        print("\nVerifying with a read-only getStoreCategories call...")
        from shoebox.clients.ebay_rest.stores import parse_store_categories

        resp = api.sell_stores_get_store_categories() or {}
        categories = parse_store_categories(resp.get("store_categories"))
        print(f"OK — {len(categories)} top-level store categories returned.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
