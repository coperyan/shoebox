"""OneDrive (Microsoft Graph) scan downloads and the MSAL token cache store."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml

from shoebox.clients.onedrive import (
    MsalTokenStore,
    OneDriveAuthError,
    OneDriveClient,
    OneDriveError,
)
from shoebox.settings import Settings, get_settings
from shoebox.utils import image_refs
from shoebox.utils.image_refs import materialize_image
from tests.test_api import FakeGCS
from tests.test_scan_path import _point_settings_at


def _settings(tmp_path, **onedrive) -> Settings:
    with open("configs/app.example.yml") as f:
        raw = yaml.safe_load(f)
    raw["onedrive"].update(
        {"client_id": "app-id", "scans_folder": "Pictures/Card Scans", **onedrive}
    )
    raw["onedrive"]["token_cache_path"] = onedrive.get(
        "token_cache_path", str(tmp_path / "cache.json")
    )
    return Settings(**raw)


class FakeCache:
    def __init__(self, text=""):
        self.text = text
        self.has_state_changed = False

    def deserialize(self, text):
        self.text = text

    def serialize(self):
        return self.text


class FakeMsalApp:
    def __init__(self, accounts=None, token="tok", fail=None):
        self.accounts = [{"username": "me@example.com"}] if accounts is None else accounts
        self.token = token
        self.fail = fail
        self.silent_calls = 0

    def get_accounts(self):
        return self.accounts

    def acquire_token_silent(self, scopes, account=None):
        self.silent_calls += 1
        if self.fail:
            return {"error": "invalid_grant", "error_description": self.fail}
        return {"access_token": f"{self.token}{self.silent_calls}"}


class FakeResponse:
    def __init__(self, status, body=b"", text=""):
        self.status_code = status
        self._body = body
        self.text = text

    def iter_content(self, chunk_size=1):
        yield self._body


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, **kwargs):
        self.calls.append((url, headers["Authorization"]))
        return self.responses.pop(0)


# ---- token store ----


def test_store_local_roundtrip_and_save_only_on_change(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    store = MsalTokenStore(settings, gcs=FakeGCS())
    cache = FakeCache('{"a": 1}')
    assert store.save(cache) is False  # unchanged -> nothing written
    assert not store.path.exists()
    cache.has_state_changed = True
    assert store.save(cache) is True
    assert store.path.read_text() == '{"a": 1}'
    assert cache.has_state_changed is False

    loaded = store.load()
    assert json.loads(loaded.serialize()) == {"a": 1}


def test_store_gs_uri_wins(tmp_path):
    gcs = FakeGCS()
    settings = _settings(tmp_path, token_cache_uri="gs://b/queue/onedrive_token_cache.json")
    store = MsalTokenStore(settings, gcs=gcs)
    assert json.loads(store.load().serialize()) == {}  # missing object -> empty cache
    cache = FakeCache('{"x": 2}')
    cache.has_state_changed = True
    store.save(cache)
    assert gcs.objects[("b", "queue/onedrive_token_cache.json")][0] == b'{"x": 2}'
    assert json.loads(store.load().serialize()) == {"x": 2}


def test_upload_local_to_uri(tmp_path):
    gcs = FakeGCS()
    settings = _settings(tmp_path, token_cache_uri="gs://b/q/onedrive_token_cache.json")
    store = MsalTokenStore(settings, gcs=gcs)
    store.path.write_text("local-cache")
    assert store.upload_local_to_uri() == "gs://b/q/onedrive_token_cache.json"
    assert gcs.objects[("b", "q/onedrive_token_cache.json")][0] == b"local-cache"
    with pytest.raises(ValueError):
        store.upload_local_to_uri("not-a-uri")


# ---- client ----


def _client(tmp_path, http, app=None):
    settings = _settings(tmp_path)
    store = MsalTokenStore(settings, gcs=FakeGCS())
    return OneDriveClient(settings, token_store=store, http=http, msal_app=app or FakeMsalApp())


def test_disabled_settings_rejected(tmp_path):
    settings = _settings(tmp_path, client_id="")
    with pytest.raises(OneDriveError):
        OneDriveClient(
            settings, token_store=MsalTokenStore(settings, gcs=FakeGCS()), http=FakeHTTP([])
        )


def test_content_url_encodes_folder_and_name(tmp_path):
    client = _client(tmp_path, FakeHTTP([]))
    assert client.content_url("SCAN_0234.jpg") == (
        "https://graph.microsoft.com/v1.0/me/drive/root:/Pictures/Card%20Scans/SCAN_0234.jpg:/content"
    )


def test_download_writes_file_with_bearer(tmp_path):
    http = FakeHTTP([FakeResponse(200, b"jpegbytes")])
    client = _client(tmp_path, http)
    dest = tmp_path / "scans" / "SCAN_0234.jpg"
    out = client.download("SCAN_0234.jpg", dest)
    assert out == dest and dest.read_bytes() == b"jpegbytes"
    assert http.calls[0][1] == "Bearer tok1"
    assert not (tmp_path / "scans" / "SCAN_0234.jpg.part").exists()
    # Token reused for the next download.
    http.responses.append(FakeResponse(200, b"x"))
    client.download("SCAN_0235.jpg", tmp_path / "scans" / "SCAN_0235.jpg")
    assert http.calls[1][1] == "Bearer tok1"


def test_download_404_is_file_not_found(tmp_path):
    client = _client(tmp_path, FakeHTTP([FakeResponse(404)]))
    with pytest.raises(FileNotFoundError, match="Pictures/Card Scans/SCAN_0001.jpg"):
        client.download("SCAN_0001.jpg", tmp_path / "x.jpg")


def test_download_401_refreshes_once(tmp_path):
    http = FakeHTTP([FakeResponse(401), FakeResponse(200, b"ok")])
    app = FakeMsalApp()
    client = _client(tmp_path, http, app)
    client.download("SCAN_0001.jpg", tmp_path / "x.jpg")
    assert [h for _, h in http.calls] == ["Bearer tok1", "Bearer tok2"]
    assert app.silent_calls == 2


def test_download_other_error(tmp_path):
    client = _client(tmp_path, FakeHTTP([FakeResponse(503, text="busy")]))
    with pytest.raises(OneDriveError, match="HTTP 503"):
        client.download("SCAN_0001.jpg", tmp_path / "x.jpg")


def test_no_account_is_auth_error(tmp_path):
    client = _client(tmp_path, FakeHTTP([]), FakeMsalApp(accounts=[]))
    with pytest.raises(OneDriveAuthError, match="onedrive_login.py --login"):
        client.access_token()


def test_refresh_failure_is_auth_error(tmp_path):
    client = _client(tmp_path, FakeHTTP([]), FakeMsalApp(fail="AADSTS70000 expired"))
    with pytest.raises(OneDriveAuthError, match="expired"):
        client.access_token()


# ---- materialize_image fallback ----


def test_materialize_fetches_missing_scan_from_onedrive(tmp_path, monkeypatch):
    _point_settings_at(
        tmp_path,
        monkeypatch,
        scans_dir=str(tmp_path / "scans"),
    )
    settings = get_settings()
    settings.onedrive.client_id = "app"
    settings.onedrive.scans_folder = "Scans"
    calls = []

    class FakeOneDrive:
        def download(self, name, dest):
            calls.append((name, dest))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"from-onedrive")
            return dest

    out = materialize_image("234", settings=settings, onedrive=FakeOneDrive())
    assert out == tmp_path / "scans" / "SCAN_0234.jpg"
    assert calls == [("SCAN_0234.jpg", out)]
    assert out.read_bytes() == b"from-onedrive"

    # Present on disk -> no download.
    out2 = materialize_image("234", settings=settings, onedrive=FakeOneDrive())
    assert out2 == out and len(calls) == 1


def test_materialize_without_onedrive_returns_guessed_path(tmp_path, monkeypatch):
    _point_settings_at(tmp_path, monkeypatch, scans_dir=str(tmp_path / "scans"))
    out = materialize_image("234", onedrive=SimpleNamespace(download=lambda *a: pytest.fail("no")))
    assert out == tmp_path / "scans" / "SCAN_0234.jpg"
    assert not out.exists()


def test_default_onedrive_client_is_shared_per_settings(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    built = []

    class FakeClient:
        def __init__(self, s):
            built.append(s)

    monkeypatch.setattr("shoebox.clients.onedrive.OneDriveClient", FakeClient)
    image_refs._onedrive_clients.clear()
    a = image_refs._default_onedrive(settings)
    b = image_refs._default_onedrive(settings)
    assert a is b and built == [settings]
