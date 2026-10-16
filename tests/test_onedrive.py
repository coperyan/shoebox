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
from shoebox.utils.image_refs import (
    MaterializedImage,
    archive_scan,
    materialize_image,
    materialize_scan,
)
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


class FakeJSONResponse(FakeResponse):
    def __init__(self, status, payload=None, text=""):
        super().__init__(status, text=text or (json.dumps(payload) if payload else ""))
        self.payload = payload or {}

    def json(self):
        return self.payload


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.requests = []  # (method, url, json body)

    def _call(self, method, url, headers=None, json=None, **kwargs):
        self.calls.append((url, headers["Authorization"]))
        self.requests.append((method, url, json))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        return self._call("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self._call("post", url, **kwargs)

    def patch(self, url, **kwargs):
        return self._call("patch", url, **kwargs)


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


# ---- move ----

SCANS = "https://graph.microsoft.com/v1.0/me/drive/root:/Pictures/Card%20Scans"


def test_move_into_existing_subfolder(tmp_path):
    http = FakeHTTP(
        [
            FakeJSONResponse(200, {"id": "FOLDER1"}),
            FakeJSONResponse(200, {"id": "ITEM", "name": "SCAN_0234.jpg"}),
            FakeJSONResponse(200, {"id": "ITEM2"}),
        ]
    )
    client = _client(tmp_path, http)
    assert client.move("SCAN_0234.jpg", "Listed") == "Pictures/Card Scans/Listed/SCAN_0234.jpg"
    assert http.requests[0] == ("get", f"{SCANS}/Listed", None)
    method, url, body = http.requests[1]
    assert (method, url) == ("patch", f"{SCANS}/SCAN_0234.jpg")
    assert body == {
        "parentReference": {"id": "FOLDER1"},
        "name": "SCAN_0234.jpg",
        "@microsoft.graph.conflictBehavior": "replace",
    }
    # The folder id is cached: a second move is one PATCH.
    client.move("SCAN_0235.jpg", "Listed")
    assert [m for m, _, _ in http.requests] == ["get", "patch", "patch"]


def test_move_creates_missing_subfolder(tmp_path):
    http = FakeHTTP(
        [
            FakeJSONResponse(404),
            FakeJSONResponse(201, {"id": "NEW"}),
            FakeJSONResponse(200, {"id": "ITEM"}),
        ]
    )
    client = _client(tmp_path, http)
    client.move("SCAN_0001.jpg", "Listed")
    method, url, body = http.requests[1]
    assert (method, url) == ("post", f"{SCANS}:/children")
    assert body == {"name": "Listed", "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}
    assert http.requests[2][2]["parentReference"] == {"id": "NEW"}


def test_move_missing_scan_is_file_not_found(tmp_path):
    http = FakeHTTP([FakeJSONResponse(200, {"id": "F"}), FakeJSONResponse(404)])
    client = _client(tmp_path, http)
    with pytest.raises(FileNotFoundError, match="SCAN_0001.jpg"):
        client.move("SCAN_0001.jpg", "Listed")


def test_move_without_write_scope_names_the_fix(tmp_path):
    http = FakeHTTP([FakeJSONResponse(403, text="accessDenied")])
    client = _client(tmp_path, http)
    with pytest.raises(OneDriveError, match="Files.ReadWrite"):
        client.move("SCAN_0001.jpg", "Listed")


def test_move_401_refreshes_once(tmp_path):
    http = FakeHTTP(
        [FakeJSONResponse(401), FakeJSONResponse(200, {"id": "F"}), FakeJSONResponse(200, {})]
    )
    app = FakeMsalApp()
    client = _client(tmp_path, http, app)
    client.move("SCAN_0001.jpg", "Listed")
    assert [h for _, h in http.calls] == ["Bearer tok1", "Bearer tok2", "Bearer tok2"]


# ---- archive_scan ----


class RecordingOneDrive:
    def __init__(self, fail=None):
        self.moves = []
        self.fail = fail

    def move(self, name, subfolder):
        if self.fail:
            raise self.fail
        self.moves.append((name, subfolder))
        return f"Scans/{subfolder}/{name}"


def _onedrive_settings(tmp_path, monkeypatch, **onedrive):
    _point_settings_at(tmp_path, monkeypatch, scans_dir=str(tmp_path / "scans"))
    settings = get_settings()
    settings.onedrive.client_id = "app"
    settings.onedrive.scans_folder = "Scans"
    for key, value in onedrive.items():
        setattr(settings.onedrive, key, value)
    return settings


def test_archive_moves_only_onedrive_sourced_scans(tmp_path, monkeypatch):
    settings = _onedrive_settings(tmp_path, monkeypatch)
    od = RecordingOneDrive()
    scan = MaterializedImage(tmp_path / "scans" / "SCAN_0234.jpg", "onedrive")
    assert archive_scan(scan, settings=settings, onedrive=od) == "Scans/Listed/SCAN_0234.jpg"
    assert od.moves == [("SCAN_0234.jpg", "Listed")]

    for source in ("local", "gs"):
        assert (
            archive_scan(MaterializedImage(scan.path, source), settings=settings, onedrive=od)
            is None
        )
    assert len(od.moves) == 1


def test_archive_off_when_subfolder_blank(tmp_path, monkeypatch):
    settings = _onedrive_settings(tmp_path, monkeypatch, listed_subfolder="")
    od = RecordingOneDrive()
    scan = MaterializedImage(tmp_path / "scans" / "SCAN_0234.jpg", "onedrive")
    assert archive_scan(scan, settings=settings, onedrive=od) is None
    assert od.moves == []


def test_archive_failure_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    settings = _onedrive_settings(tmp_path, monkeypatch)
    od = RecordingOneDrive(fail=OneDriveError("HTTP 403"))
    scan = MaterializedImage(tmp_path / "scans" / "SCAN_0234.jpg", "onedrive")
    with caplog.at_level("WARNING"):
        assert archive_scan(scan, settings=settings, onedrive=od) is None
    assert "SCAN_0234.jpg" in caplog.text and "Listed" in caplog.text


# ---- materialize_image fallback ----


def test_materialize_scan_reports_source(tmp_path, monkeypatch):
    settings = _onedrive_settings(tmp_path, monkeypatch)

    class FakeOneDrive:
        def download(self, name, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x")
            return dest

    fetched = materialize_scan("234", settings=settings, onedrive=FakeOneDrive())
    assert fetched.source == "onedrive"
    assert fetched.path == tmp_path / "scans" / "SCAN_0234.jpg"
    # Now on disk: a local file, so a later archive_scan leaves OneDrive alone.
    assert materialize_scan("234", settings=settings, onedrive=FakeOneDrive()).source == "local"

    def fake_download(uri, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"")
        return dest

    gs = materialize_scan("gs://b/uploads/p.jpg", settings=settings, download=fake_download)
    assert gs.source == "gs"


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


def test_concurrent_token_requests_refresh_once(tmp_path):
    """create-listings downloads from several threads: one silent acquisition
    serves them all, and a 401 retry from two threads refreshes only once."""
    import threading
    import time

    class SlowApp(FakeMsalApp):
        def acquire_token_silent(self, scopes, account=None):
            time.sleep(0.05)
            return super().acquire_token_silent(scopes, account)

    app = SlowApp()
    client = _client(tmp_path, http=None, app=app)
    tokens: list[str] = []
    start = threading.Barrier(2)

    def grab(fresh=False):
        if fresh:
            start.wait()
        tokens.append(client.access_token(fresh=fresh))

    threads = [threading.Thread(target=grab) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert app.silent_calls == 1 and set(tokens) == {"tok1"}

    tokens.clear()
    threads = [threading.Thread(target=grab, kwargs={"fresh": True}) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert app.silent_calls == 2 and set(tokens) == {"tok2"}
