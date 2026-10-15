"""Sign in to OneDrive once so create-listings can fetch scans from it.

Workstation-only (it needs a browser, on any device). Writes the MSAL token
cache to ``onedrive.token_cache_path`` and, with ``--upload``, copies it to
``onedrive.token_cache_uri`` for the Cloud Run job started from the iOS app.

    python scripts/onedrive_login.py --login            # device-code sign-in
    python scripts/onedrive_login.py --check            # who is signed in; can a token be refreshed?
    python scripts/onedrive_login.py --verify 234       # download SCAN_0234.jpg to a temp dir
    python scripts/onedrive_login.py --upload           # copy the cache to the gs:// object

The refresh token expires after about 90 days without use; when the job
starts failing with OneDriveAuthError, run --login --reset and --upload again.
See docs/deployment.md, "Listing from the app".
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--login", action="store_true", help="run the device-code sign-in")
    parser.add_argument(
        "--reset", action="store_true", help="with --login: replace an existing account"
    )
    parser.add_argument(
        "--check", action="store_true", help="show the signed-in account and refresh a token"
    )
    parser.add_argument(
        "--verify", metavar="SCAN_NUMBER", help="download one scan to prove scans_folder is right"
    )
    parser.add_argument(
        "--upload", action="store_true", help="copy the local cache to onedrive.token_cache_uri"
    )
    args = parser.parse_args()
    if not (args.login or args.check or args.verify or args.upload):
        parser.print_help()
        return 1

    from shoebox.clients.onedrive import MsalTokenStore, OneDriveAuthError, OneDriveClient
    from shoebox.settings import get_settings
    from shoebox.ui.helpers import handle_image_path

    settings = get_settings()
    cfg = settings.onedrive
    if not cfg.enabled:
        print("error: set onedrive.client_id and onedrive.scans_folder in configs/app.yaml first")
        return 1

    # The local file is the source of truth here; --upload pushes it to GCS.
    local_only = settings.model_copy(deep=True)
    local_only.onedrive.token_cache_uri = None
    store = MsalTokenStore(local_only)

    if args.login:
        from msal import PublicClientApplication

        cache = store.load()
        app = PublicClientApplication(cfg.client_id, authority=cfg.authority, token_cache=cache)
        if app.get_accounts() and not args.reset:
            print(
                f"already signed in as {app.get_accounts()[0].get('username')}; use --reset to replace"
            )
            return 1
        for account in app.get_accounts():
            app.remove_account(account)
        flow = app.initiate_device_flow(scopes=list(cfg.scopes))
        if "user_code" not in flow:
            print(f"error: could not start the device-code flow: {flow}")
            return 1
        print(flow["message"])
        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            print(f"error: sign-in failed: {result.get('error_description') or result}")
            return 1
        store.save(cache, force=True)
        print(
            f"signed in as {app.get_accounts()[0].get('username')}; cache written to {store.path}"
        )

    if args.check or args.verify:
        client = OneDriveClient(local_only, token_store=store)
        try:
            client.access_token()
        except OneDriveAuthError as exc:
            print(f"error: {exc}")
            return 1
        print(
            f"signed in as {client.account_name()}; token refresh OK; folder {cfg.scans_folder!r}"
        )
        if args.verify:
            name = Path(handle_image_path(args.verify)).name
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    path = client.download(name, Path(tmp) / name)
                except FileNotFoundError as exc:
                    print(f"error: {exc}")
                    return 1
                print(f"downloaded {name} ({path.stat().st_size} bytes) from {cfg.scans_folder}")

    if args.upload:
        if not cfg.token_cache_uri:
            print("error: set onedrive.token_cache_uri (gs://...) in configs/app.yaml first")
            return 1
        uri = MsalTokenStore(local_only).upload_local_to_uri(cfg.token_cache_uri)
        print(f"uploaded {store.path} to {uri}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
