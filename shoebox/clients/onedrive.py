"""Card scans from OneDrive through Microsoft Graph.

The workstation reads scans from the OneDrive *sync folder* on disk. The
Cloud Run ``create-listings`` job started from the iOS app has no such folder,
so it fetches ``SCAN_0234.jpg`` from OneDrive itself:

    GET https://graph.microsoft.com/v1.0/me/drive/root:/<scans_folder>/<name>:/content

Once such a scan has been uploaded, the job moves it into
``<scans_folder>/<listed_subfolder>`` (PATCH the item's ``parentReference``),
which is what deleting the local original does on the workstation: the scans
folder only ever holds cards that still need listing.

Auth is MSAL's device-code flow, done once on the workstation by
``scripts/onedrive_login.py``. The resulting token cache (a refresh token) is
kept as a local file and, for the job, as a GCS object named by
``onedrive.token_cache_uri``. MSAL rotates refresh tokens on use, so the cache
is written back after every token acquisition; a read-only secret mount would
go stale, which is why it lives in GCS and not Secret Manager.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from shoebox.clients.gcs import GCSClient, is_gs_uri, parse_gs_uri
from shoebox.settings import Settings, get_settings

logger = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
DOWNLOAD_TIMEOUT_S = 60


class OneDriveError(RuntimeError):
    """Graph returned something other than the file."""


class OneDriveAuthError(OneDriveError):
    """No usable account/refresh token in the cache."""


class MsalTokenStore:
    """Loads and saves the MSAL ``SerializableTokenCache``.

    ``token_cache_uri`` (gs://) wins when set; otherwise the local
    ``token_cache_path``. Saving is a no-op unless MSAL reports the cache
    changed, so a plain download never rewrites the object.
    """

    def __init__(self, settings: Settings | None = None, *, gcs: Any = None):
        self.settings = settings or get_settings()
        self.uri = self.settings.onedrive.token_cache_uri
        self.path = Path(self.settings.onedrive.token_cache_path)
        self._gcs = gcs

    @property
    def gcs(self) -> Any:
        if self._gcs is None:
            self._gcs = GCSClient(settings=self.settings)
        return self._gcs

    @property
    def location(self) -> str:
        return self.uri or str(self.path)

    def load(self):
        from msal import SerializableTokenCache

        cache = SerializableTokenCache()
        try:
            if self.uri:
                bucket, name = parse_gs_uri(self.uri)
                text = self.gcs.read_text(bucket=bucket, object_name=name)
            else:
                text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cache
        if text.strip():
            cache.deserialize(text)
        return cache

    def save(self, cache, *, force: bool = False) -> bool:
        if not force and not cache.has_state_changed:
            return False
        text = cache.serialize()
        if self.uri:
            bucket, name = parse_gs_uri(self.uri)
            self.gcs.upload_text(
                bucket=bucket, object_name=name, text=text, content_type="application/json"
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.path)
        cache.has_state_changed = False
        return True

    def upload_local_to_uri(self, uri: str | None = None) -> str:
        """Copy the workstation cache to the gs:// object the job reads."""
        uri = uri or self.uri
        if not uri or not is_gs_uri(uri):
            raise ValueError("onedrive.token_cache_uri (gs://...) is not set")
        text = self.path.read_text(encoding="utf-8")
        bucket, name = parse_gs_uri(uri)
        self.gcs.upload_text(
            bucket=bucket, object_name=name, text=text, content_type="application/json"
        )
        return uri


class OneDriveClient:
    """Downloads scans by file name from ``onedrive.scans_folder`` and moves
    listed ones into its ``listed_subfolder``."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        token_store: MsalTokenStore | None = None,
        http: Any = None,
        msal_app: Any = None,
    ):
        self.settings = settings or get_settings()
        self.config = self.settings.onedrive
        if not self.config.enabled:
            raise OneDriveError("onedrive.client_id and onedrive.scans_folder are not set")
        self.token_store = token_store or MsalTokenStore(self.settings)
        self.http = http or requests
        self._app = msal_app
        self._cache = None
        self._access_token: str | None = None
        self._folder_ids: dict[str, str] = {}

    # -----------------------------
    # Auth
    # -----------------------------
    def _application(self):
        if self._app is None:
            from msal import PublicClientApplication

            self._cache = self.token_store.load()
            self._app = PublicClientApplication(
                self.config.client_id, authority=self.config.authority, token_cache=self._cache
            )
        return self._app

    def access_token(self, *, fresh: bool = False) -> str:
        if self._access_token and not fresh:
            return self._access_token
        app = self._application()
        accounts = app.get_accounts()
        if not accounts:
            raise OneDriveAuthError(
                f"no OneDrive account in the token cache at {self.token_store.location}; "
                "run `python scripts/onedrive_login.py --login` (then --upload for the cloud)"
            )
        result = app.acquire_token_silent(list(self.config.scopes), account=accounts[0])
        if self._cache is not None:
            self.token_store.save(self._cache)
        if not result or "access_token" not in result:
            detail = (result or {}).get("error_description") or (result or {}).get("error")
            raise OneDriveAuthError(
                "could not refresh the OneDrive token"
                + (f": {detail}" if detail else "")
                + "; sign in again with `python scripts/onedrive_login.py --login --reset`"
            )
        self._access_token = result["access_token"]
        return self._access_token

    def account_name(self) -> str | None:
        accounts = self._application().get_accounts()
        return accounts[0].get("username") if accounts else None

    # -----------------------------
    # Files
    # -----------------------------
    def _folder_path(self, *parts: str) -> str:
        segments = self.config.scans_folder.strip("/").split("/")
        segments += [part for p in parts for part in p.strip("/").split("/") if part]
        return "/".join(quote(part) for part in segments if part)

    def item_url(self, name: str, *, subfolder: str = "") -> str:
        """The driveItem itself (metadata, PATCH)."""
        return f"{GRAPH_ROOT}/me/drive/root:/{self._folder_path(subfolder)}/{quote(name)}"

    def content_url(self, name: str) -> str:
        return f"{self.item_url(name)}:/content"

    def download(self, name: str, dest: Path) -> Path:
        """Fetch ``<scans_folder>/<name>`` into ``dest`` (parents created).

        Graph answers with a redirect to a pre-authenticated download URL;
        ``requests`` follows it and drops the bearer header on the way, as it
        should. A missing file is FileNotFoundError, like a local path.
        """
        dest = Path(dest)
        url = self.content_url(name)
        response = self._get(url, self.access_token())
        if response.status_code == 401:
            response = self._get(url, self.access_token(fresh=True))
        if response.status_code == 404:
            raise FileNotFoundError(f"OneDrive: {self.config.scans_folder}/{name} not found")
        if response.status_code >= 400:
            raise OneDriveError(
                f"OneDrive download of {name} failed: HTTP {response.status_code} "
                f"{(response.text or '')[:200]}"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        with tmp.open("wb") as f:
            for chunk in response.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
        tmp.replace(dest)
        logger.info("Downloaded %s from OneDrive (%d bytes)", name, dest.stat().st_size)
        return dest

    def move(self, name: str, subfolder: str) -> str:
        """Move ``<scans_folder>/<name>`` into ``<scans_folder>/<subfolder>``,
        creating the subfolder on first use; returns the new OneDrive path.

        A same-named file already there is replaced (a re-run of the same
        queue). Missing source is FileNotFoundError; a token without
        ``Files.ReadWrite`` surfaces as OneDriveError (HTTP 403).
        """
        subfolder = subfolder.strip("/")
        if not subfolder:
            raise ValueError("subfolder is empty")
        folder_id = self._folder_id(subfolder)
        body = {
            "parentReference": {"id": folder_id},
            "name": name,
            "@microsoft.graph.conflictBehavior": "replace",
        }
        response = self._json("patch", self.item_url(name), json=body)
        if response.status_code == 404:
            raise FileNotFoundError(f"OneDrive: {self.config.scans_folder}/{name} not found")
        self._raise_for(response, f"move of {name} to {subfolder}")
        new_path = f"{self.config.scans_folder.strip('/')}/{subfolder}/{name}"
        logger.info("Moved %s to OneDrive %s", name, new_path)
        return new_path

    def _folder_id(self, subfolder: str) -> str:
        cached = self._folder_ids.get(subfolder)
        if cached:
            return cached
        response = self._json("get", self.item_url(subfolder))
        if response.status_code == 404:
            parent, _, leaf = subfolder.rpartition("/")
            response = self._json(
                "post",
                f"{GRAPH_ROOT}/me/drive/root:/{self._folder_path(parent)}:/children",
                json={"name": leaf, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
            )
            if response.status_code == 409:  # created meanwhile; look it up
                response = self._json("get", self.item_url(subfolder))
        self._raise_for(response, f"lookup of folder {subfolder}")
        folder_id = (response.json() or {}).get("id")
        if not folder_id:
            raise OneDriveError(f"OneDrive folder {subfolder} has no id in {response.text[:200]}")
        self._folder_ids[subfolder] = folder_id
        return folder_id

    def _raise_for(self, response, what: str) -> None:
        if response.status_code >= 400:
            hint = (
                " (the token lacks Files.ReadWrite: run `python scripts/onedrive_login.py "
                "--login --reset`, then --upload)"
                if response.status_code == 403
                else ""
            )
            raise OneDriveError(
                f"OneDrive {what} failed: HTTP {response.status_code} "
                f"{(response.text or '')[:200]}{hint}"
            )

    def _json(self, method: str, url: str, *, json: dict | None = None):
        """One Graph JSON call, with a single retry on 401 after a fresh token."""
        response = self._request(method, url, self.access_token(), json=json)
        if response.status_code == 401:
            response = self._request(method, url, self.access_token(fresh=True), json=json)
        return response

    def _request(self, method: str, url: str, token: str, *, json: dict | None = None):
        return getattr(self.http, method)(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=json,
            timeout=DOWNLOAD_TIMEOUT_S,
        )

    def _get(self, url: str, token: str):
        return self.http.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            allow_redirects=True,
            stream=True,
            timeout=DOWNLOAD_TIMEOUT_S,
        )
