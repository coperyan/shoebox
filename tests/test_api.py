"""The listing-queue API, end to end against in-memory metadata and a fake GCS."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient

from shoebox.api.app import create_app
from shoebox.api.queue_store import draft_uri_for
from shoebox.settings import Settings

QUEUE_URI = "gs://ebay-bucket/queue/listing_queue_enriched.jsonl"
KEY = "test-key"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _settings() -> Settings:
    with open("configs/app.example.yml") as f:
        raw = yaml.safe_load(f)
    raw["paths"]["queue_uri"] = QUEUE_URI
    raw["gcs"]["image_bucket"] = "img-bucket"
    return Settings(**raw)


def _frames():
    cdf = pd.DataFrame(
        [
            (
                "2024 Set",
                "Base",
                "Base",
                "1",
                "1",
                "Player One",
                "Team A",
                "RC",
                "RC",
                "2024",
                "S|B|1",
                "S|B",
            ),
            (
                "2024 Set",
                "Base",
                "Base",
                "2",
                "2",
                "Player Two",
                None,
                None,
                None,
                "2024",
                "S|B|2",
                "S|B",
            ),
            (
                "2024 Set",
                "Stars",
                "Insert",
                "A",
                "A",
                "Star",
                "Team A",
                None,
                "RC",
                "2024",
                "S|St|A",
                "S|St",
            ),
        ],
        columns=[
            "set_name",
            "subset_name",
            "subset_type",
            "derived_card_number",
            "card_number",
            "player",
            "team",
            "note",
            "note_check",
            "set_year",
            "card_id",
            "subset_id",
        ],
    )
    pdf = pd.DataFrame(
        [("2024 Set", "Base", "Gold", 50, "SP", "S|B|Gold")],
        columns=[
            "set_name",
            "subset_name",
            "parallel_variety",
            "print_run",
            "other_note",
            "parallel_id",
        ],
    )
    return cdf, pdf


class FakeGCS:
    def __init__(self):
        self.objects: dict[tuple[str, str], tuple[bytes, str, dict | None]] = {}

    def upload_text(self, *, bucket, object_name, text, content_type="text/plain"):
        self.objects[(bucket, object_name)] = (text.encode("utf-8"), content_type, None)

    def upload_bytes(self, *, bucket, object_name, data, content_type, metadata=None):
        self.objects[(bucket, object_name)] = (data, content_type, metadata)
        return f"gs://{bucket}/{object_name}"

    def read_text(self, *, bucket, object_name):
        try:
            return self.objects[(bucket, object_name)][0].decode("utf-8")
        except KeyError:
            raise FileNotFoundError(object_name) from None


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


@pytest.fixture
def env():
    gcs = FakeGCS()
    clock = FakeClock()
    loads = []

    def loader():
        loads.append(1)
        return _frames()

    app = create_app(
        settings=_settings(),
        api_key=KEY,
        metadata_loader=loader,
        gcs=gcs,
        metadata_ttl_s=3600,
        clock=clock,
    )
    client = TestClient(app)
    client.headers.update({"X-API-Key": KEY})
    return client, gcs, clock, loads


def row(**over):
    base = {
        "set_name": "2024 Set",
        "subset_name": "Base",
        "parallel_variety": None,
        "card_number": "1",
        "quantity": 1,
        "price": 4.99,
        "image_front": "123",
        "image_back": "124",
    }
    base.update(over)
    return base


# ---- startup guards ----


def test_create_app_requires_key(monkeypatch):
    monkeypatch.delenv("SHOEBOX_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="SHOEBOX_API_KEY"):
        create_app(settings=_settings(), metadata_loader=_frames, gcs=FakeGCS())


def test_create_app_requires_queue_uri():
    s = _settings()
    s.paths.queue_uri = None
    with pytest.raises(RuntimeError, match="queue_uri"):
        create_app(settings=s, api_key=KEY, metadata_loader=_frames, gcs=FakeGCS())


def test_create_app_reads_key_from_env(monkeypatch):
    monkeypatch.setenv("SHOEBOX_API_KEY", "from-env")
    app = create_app(settings=_settings(), metadata_loader=_frames, gcs=FakeGCS())
    c = TestClient(app)
    assert c.get("/queue", headers={"X-API-Key": "from-env"}).status_code == 200


# ---- auth ----


@pytest.mark.parametrize("path", ["/health", "/healthz"])
def test_health_needs_no_key(env, path):
    client, *_ = env
    r = TestClient(client.app).get(path)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/metadata"),
        ("POST", "/metadata/refresh"),
        ("POST", "/queue/preview"),
        ("PUT", "/queue"),
        ("PUT", "/queue/draft"),
        ("GET", "/queue"),
        ("POST", "/images"),
    ],
)
def test_routes_reject_missing_or_wrong_key(env, method, path):
    client, *_ = env
    bare = TestClient(client.app)
    assert bare.request(method, path).status_code == 401
    assert bare.request(method, path, headers={"X-API-Key": "nope"}).status_code == 401


# ---- metadata ----


def test_metadata_shape_and_etag(env):
    client, _, _, loads = env
    r = client.get("/metadata")
    assert r.status_code == 200
    body = r.json()
    assert body["counts"] == {"checklist_rows": 3, "parallel_rows": 1, "sets": 1}
    assert body["ttl_s"] == 3600
    assert r.headers["ETag"] == f'W/"{body["version"]}"'
    s = body["sets"][0]
    assert s["set_name"] == "2024 Set"
    assert [x["subset_name"] for x in s["subsets"]] == ["Base", "Stars"]
    base = s["subsets"][0]
    assert [c["card_number"] for c in base["cards"]] == ["1", "2"]
    assert base["cards"][0]["player"] == "Player One"
    assert base["cards"][1]["team"] is None
    assert base["parallels"] == [
        {"parallel_variety": "Gold", "parallel_id": "S|B|Gold", "print_run": 50, "other_note": "SP"}
    ]
    assert loads == [1]


def test_metadata_304_on_matching_etag(env):
    client, _, _, loads = env
    etag = client.get("/metadata").headers["ETag"]
    r = client.get("/metadata", headers={"If-None-Match": etag})
    assert r.status_code == 304
    assert r.headers["ETag"] == etag
    assert loads == [1]  # cached, no reload


def test_metadata_ttl_and_refresh(env):
    client, _, clock, loads = env
    client.get("/metadata")
    client.get("/metadata")
    assert loads == [1]
    clock.now += timedelta(seconds=3601)
    client.get("/metadata")
    assert loads == [1, 1]
    r = client.post("/metadata/refresh")
    assert r.status_code == 200
    assert set(r.json()) == {"version", "generated_at", "counts"}
    assert loads == [1, 1, 1]


def test_metadata_503_when_loader_fails_first_time():
    def boom():
        raise RuntimeError("bq down")

    app = create_app(settings=_settings(), api_key=KEY, metadata_loader=boom, gcs=FakeGCS())
    c = TestClient(app)
    r = c.get("/metadata", headers={"X-API-Key": KEY})
    assert r.status_code == 503
    assert "bq down" in r.json()["detail"]


def test_metadata_keeps_stale_bundle_when_reload_fails():
    calls = []

    def loader():
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("bq down")
        return _frames()

    clock = FakeClock()
    app = create_app(
        settings=_settings(), api_key=KEY, metadata_loader=loader, gcs=FakeGCS(), clock=clock
    )
    c = TestClient(app)
    c.headers.update({"X-API-Key": KEY})
    assert c.get("/metadata").status_code == 200
    clock.now += timedelta(hours=2)
    assert c.get("/metadata").status_code == 200  # stale served
    assert calls == [1, 1]


# ---- preview ----


def test_preview_ok_error_and_warnings(env):
    client, *_ = env
    rows = [
        row(),
        row(card_number="99"),
        row(parallel_variety="Gold", image_back=""),
        row(parallel_variety="Silver"),
        row(),
    ]
    r = client.post("/queue/preview", json={"rows": rows})
    assert r.status_code == 200
    body = r.json()
    assert body["counts"] == {"rows": 5, "ok": 4, "errors": 1}
    assert body["metadata_version"]
    res = body["rows"]
    assert res[0]["ok"] and res[0]["enriched"]["player"] == "Player One"
    assert res[0]["enriched"]["card_id"] == "S|B|1"
    assert res[0]["enriched"]["image_front"] == "123"
    assert len(res[0]["enriched"]) == 21
    assert res[1] == {
        "index": 1,
        "ok": False,
        "enriched": None,
        "error": "No checklist match",
        "warnings": [],
    }
    assert res[2]["enriched"]["print_run"] == 50
    assert res[2]["enriched"]["card_id"] == "2024 Set|Base|Gold|1"
    assert res[2]["warnings"] == ["image_back missing"]
    assert res[3]["ok"] and res[3]["enriched"]["parallel_variety"] is None
    assert any("not in parallels view" in w for w in res[3]["warnings"])
    assert res[4]["warnings"] == ["duplicate of row 0"]


def test_preview_validates_schema(env):
    client, *_ = env
    assert client.post("/queue/preview", json={"rows": []}).status_code == 422
    assert client.post("/queue/preview", json={"rows": [row(quantity=0)]}).status_code == 422
    assert client.post("/queue/preview", json={"rows": [row(set_name="")]}).status_code == 422
    assert client.post("/queue/preview", json={"rows": [row(extra=1)]}).status_code == 422


# ---- queue write / read ----


def test_put_queue_writes_jsonl_and_draft(env):
    client, gcs, clock, _ = env
    rows = [row(), row(card_number="2", image_back="gs://img-bucket/uploads/2026-10/abc.jpg")]
    r = client.put("/queue", json={"rows": rows})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "queue"
    assert body["counts"] == {"rows": 2, "ok": 2, "errors": 0}
    assert body["queue_uri"] == QUEUE_URI
    assert body["draft_uri"] == "gs://ebay-bucket/queue/listing_queue_draft.json"
    assert body["submitted_at"].startswith("2026-10-15T12:00:00")

    data, ctype, _ = gcs.objects[("ebay-bucket", "queue/listing_queue_enriched.jsonl")]
    assert ctype == "application/json"
    lines = [json.loads(line) for line in data.decode().splitlines()]
    assert len(lines) == 2
    assert lines[0]["image_front"] == "123"  # bare scan number kept verbatim
    assert lines[1]["image_back"] == "gs://img-bucket/uploads/2026-10/abc.jpg"
    assert lines[0]["set_year"] == "2024" and lines[0]["player"] == "Player One"
    assert set(lines[0]) == {
        "card_id",
        "checklist_id",
        "subset_id",
        "parallel_id",
        "set_year",
        "set_name",
        "subset_name",
        "subset_type",
        "parallel_variety",
        "derived_card_number",
        "card_number",
        "player",
        "team",
        "note",
        "note_check",
        "print_run",
        "parallel_note",
        "quantity",
        "price",
        "image_front",
        "image_back",
    }

    draft = json.loads(gcs.objects[("ebay-bucket", "queue/listing_queue_draft.json")][0])
    assert draft["kind"] == "queue"
    assert [r["card_number"] for r in draft["rows"]] == ["1", "2"]

    g = client.get("/queue").json()
    assert g["kind"] == "queue"
    assert g["submitted_at"].startswith("2026-10-15T12:00:00")
    assert [r["card_number"] for r in g["rows"]] == ["1", "2"]
    assert g["rows"][0]["image_front"] == "123"
    assert g["counts"]["rows"] == 2


def test_put_queue_with_error_row_writes_nothing(env):
    client, gcs, *_ = env
    r = client.put("/queue", json={"rows": [row(), row(card_number="99")]})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["message"] == "1 rows failed enrichment"
    assert detail["rows"][0]["index"] == 1
    assert detail["rows"][0]["error"] == "No checklist match"
    assert gcs.objects == {}


def test_put_draft_allows_blank_rows_and_get_roundtrip(env):
    client, gcs, *_ = env
    rows = [row(), {"set_name": "2024 Set", "subset_name": "", "card_number": ""}]
    r = client.put("/queue/draft", json={"rows": rows})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "draft"
    assert r.json()["queue_uri"] is None
    assert ("ebay-bucket", "queue/listing_queue_enriched.jsonl") not in gcs.objects
    g = client.get("/queue").json()
    assert g["kind"] == "draft"
    assert g["rows"][1]["subset_name"] == ""
    assert g["rows"][1]["quantity"] == 1

    # Empty draft clears it.
    assert client.put("/queue/draft", json={"rows": []}).status_code == 200
    assert client.get("/queue").json()["rows"] == []


def test_get_queue_empty_state(env):
    client, *_ = env
    g = client.get("/queue").json()
    assert g == {
        "kind": None,
        "submitted_at": None,
        "rows": [],
        "counts": {"rows": 0, "ok": 0, "errors": 0},
        "queue_uri": QUEUE_URI,
        "draft_uri": "gs://ebay-bucket/queue/listing_queue_draft.json",
    }


def test_draft_uri_for():
    assert draft_uri_for("gs://b/q.jsonl") == "gs://b/listing_queue_draft.json"
    assert draft_uri_for("gs://b/a/b/q.jsonl") == "gs://b/a/b/listing_queue_draft.json"


# ---- images ----


def _upload(client, data, side="front", client_id=None, filename="x.jpg", ctype="image/jpeg"):
    form = {"side": side}
    if client_id is not None:
        form["client_id"] = client_id
    return client.post("/images", files={"file": (filename, data, ctype)}, data=form)


def test_image_jpeg_upload(env):
    client, gcs, *_ = env
    r = _upload(client, JPEG)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["uri"].startswith("gs://img-bucket/uploads/2026-10/")
    assert body["uri"].endswith(".jpg")
    assert body["side"] == "front"
    assert body["client_id"] is None
    assert body["bytes"] == len(JPEG)
    assert body["content_type"] == "image/jpeg"
    name = body["uri"][len("gs://img-bucket/") :]
    data, ctype, meta = gcs.objects[("img-bucket", name)]
    assert data == JPEG and ctype == "image/jpeg" and meta == {"side": "front"}


def test_image_png_and_declared_type_ignored(env):
    client, gcs, *_ = env
    r = _upload(client, PNG, side="back", ctype="image/jpeg", filename="x.jpg")
    assert r.status_code == 201
    assert r.json()["content_type"] == "image/png"
    assert r.json()["uri"].endswith(".png")


def test_image_client_id_is_idempotent(env):
    client, gcs, *_ = env
    a = _upload(client, JPEG, client_id="ABCDEFGH-1234").json()["uri"]
    b = _upload(client, JPEG, client_id="ABCDEFGH-1234").json()["uri"]
    assert a == b == "gs://img-bucket/uploads/2026-10/ABCDEFGH-1234.jpg"
    assert len(gcs.objects) == 1
    assert gcs.objects[("img-bucket", "uploads/2026-10/ABCDEFGH-1234.jpg")][2] == {
        "side": "front",
        "client_id": "ABCDEFGH-1234",
    }


def test_image_rejections(env):
    client, gcs, *_ = env
    assert _upload(client, b"hello world, not an image").status_code == 415
    assert _upload(client, JPEG, side="left").status_code == 422
    assert _upload(client, JPEG, client_id="short").status_code == 422
    assert _upload(client, JPEG, client_id="bad id with spaces").status_code == 422
    assert client.post("/images", data={"side": "front"}).status_code == 422
    big = JPEG + b"\x00" * (10 * 1024 * 1024)
    assert _upload(client, big).status_code == 413
    assert gcs.objects == {}


# ---- listing runs (POST/GET /queue/listings) ----


class FakeJobRunner:
    def __init__(self, fail=False):
        self.fail = fail
        self.started: list[str] = []

    def run_job(self, name):
        self.started.append(name)
        if self.fail:
            raise RuntimeError("403 run.jobs.run denied")
        return f"projects/p/locations/r/jobs/{name}/executions/{name}-abc12"


@pytest.fixture
def run_env():
    from shoebox.services.listing_runs import ListingRunStore
    from tests.test_listing_runs import FakeBucket

    gcs = FakeGCS()
    clock = FakeClock()
    bucket = FakeBucket()
    runner = FakeJobRunner()
    app = create_app(
        settings=_settings(),
        api_key=KEY,
        metadata_loader=_frames,
        gcs=gcs,
        clock=clock,
        job_runner=runner,
        run_store=ListingRunStore(bucket, "queue/runs/"),
    )
    client = TestClient(app)
    client.headers.update({"X-API-Key": KEY})
    return client, gcs, clock, bucket, runner


def test_start_run_requires_a_submitted_queue(run_env):
    client, *_ = run_env
    r = client.post("/queue/listings", json={"publish": True, "schedule": True})
    assert r.status_code == 409
    assert "submit the queue first" in r.json()["detail"]["message"]
    client.put("/queue/draft", json={"rows": [row()]})
    r = client.post("/queue/listings", json={})
    assert r.status_code == 409


def test_start_run_writes_record_and_starts_job(run_env):
    client, gcs, clock, bucket, runner = run_env
    client.put("/queue", json={"rows": [row(), row(card_number="2")]})
    r = client.post("/queue/listings", json={"publish": False, "schedule": True})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["state"] == "requested"
    assert body["run_id"].startswith("20261015T120000Z-")
    assert body["publish"] is False and body["schedule"] is True
    assert body["rows_total"] == 2
    assert body["queue_uri"] == QUEUE_URI
    assert body["metadata_version"]
    assert body["execution"].endswith("create-listings-abc12")
    assert body["results"] == []
    assert runner.started == ["create-listings"]
    assert f"queue/runs/{body['run_id']}.json" in bucket.objects

    # Visible through GET, and listed newest-first without results.
    one = client.get(f"/queue/listings/{body['run_id']}").json()
    assert one["run_id"] == body["run_id"] and one["state"] == "requested"
    runs = client.get("/queue/listings").json()["runs"]
    assert [x["run_id"] for x in runs] == [body["run_id"]]
    assert "results" not in runs[0]
    assert "scrape" not in r.text


def test_start_run_rejected_while_one_is_active(run_env):
    client, gcs, clock, bucket, runner = run_env
    client.put("/queue", json={"rows": [row()]})
    first = client.post("/queue/listings", json={}).json()
    r = client.post("/queue/listings", json={})
    assert r.status_code == 409
    assert r.json()["detail"] == {"message": "a run is in progress", "run_id": first["run_id"]}
    assert runner.started == ["create-listings"]

    # After the job finished (record updated) a new run is allowed again.
    from shoebox.services.listing_runs import ListingRun

    rec = ListingRun.model_validate_json(bucket.objects[f"queue/runs/{first['run_id']}.json"][0])
    rec.state = "done"
    bucket.blob(f"queue/runs/{first['run_id']}.json").upload_from_string(rec.model_dump_json())
    clock.now += timedelta(seconds=1)
    assert client.post("/queue/listings", json={}).status_code == 202
    # And a stale request no longer blocks.
    clock.now += timedelta(hours=2)
    assert client.post("/queue/listings", json={}).status_code == 202


def test_start_run_job_failure_is_502_and_recorded(run_env):
    client, gcs, clock, bucket, runner = run_env
    runner.fail = True
    client.put("/queue", json={"rows": [row()]})
    r = client.post("/queue/listings", json={})
    assert r.status_code == 502
    assert "could not start the Cloud Run job" in r.json()["detail"]
    runs = client.get("/queue/listings").json()["runs"]
    assert runs[0]["state"] == "failed" and "403" in runs[0]["error"]
    # A failed start does not block the next attempt.
    runner.fail = False
    clock.now += timedelta(seconds=1)
    assert client.post("/queue/listings", json={}).status_code == 202


def test_start_run_validates_body_and_unknown_run_404(run_env):
    client, *_ = run_env
    assert client.post("/queue/listings", json={"scrape_prices": True}).status_code == 422
    assert client.get("/queue/listings/nope").status_code == 404


def test_listing_routes_need_key(run_env):
    client, *_ = run_env
    bare = TestClient(client.app)
    assert bare.post("/queue/listings", json={}).status_code == 401
    assert bare.get("/queue/listings").status_code == 401
    assert bare.get("/queue/listings/x").status_code == 401


def test_api_key_whitespace_is_ignored(monkeypatch):
    # The secret stored with a trailing newline (openssl | gcloud --data-file=-) must still match.
    monkeypatch.setenv("SHOEBOX_API_KEY", "from-env\n")
    app = create_app(settings=_settings(), metadata_loader=_frames, gcs=FakeGCS())
    c = TestClient(app)
    assert c.get("/queue", headers={"X-API-Key": "from-env"}).status_code == 200
    monkeypatch.setenv("SHOEBOX_API_KEY", "  \n")
    with pytest.raises(RuntimeError, match="SHOEBOX_API_KEY"):
        create_app(settings=_settings(), metadata_loader=_frames, gcs=FakeGCS())
