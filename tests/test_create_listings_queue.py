"""create-listings queue source: local file, gs:// object, --queue precedence."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shoebox.models.listing_queue import ListingQueueRow
from shoebox.pipelines import create_listings as cl
from shoebox.settings import get_settings
from tests.test_scan_path import _point_settings_at

ROW = {
    "card_id": "S|Base|1",
    "set_year": "2024",
    "set_name": "S",
    "subset_name": "Base",
    "subset_type": "Base",
    "card_number": "1",
    "player": "P",
    "quantity": 1,
    "price": 2.99,
    "image_front": "123",
    "image_back": "gs://b/uploads/x.jpg",
}


def test_read_queue_local(tmp_path):
    path = tmp_path / "q.jsonl"
    path.write_text(json.dumps(ROW) + "\n\n" + json.dumps({**ROW, "card_number": "2"}) + "\n")
    rows = cl.read_queue(path)
    assert [r.card_number for r in rows] == ["1", "2"]
    assert rows[0].image_front == "123"
    assert rows[0].image_back == "gs://b/uploads/x.jpg"


def test_read_queue_missing_local(tmp_path):
    with pytest.raises(FileNotFoundError):
        cl.read_queue(tmp_path / "nope.jsonl")


def test_read_queue_gs(monkeypatch):
    seen = {}

    def fake_read(uri):
        seen["uri"] = uri
        return json.dumps(ROW) + "\n" + json.dumps({**ROW, "card_number": "3"}) + "\n"

    monkeypatch.setattr(cl, "read_gs_text", fake_read)
    rows = cl.read_queue("gs://bucket/queue/listing_queue_enriched.jsonl")
    assert seen["uri"] == "gs://bucket/queue/listing_queue_enriched.jsonl"
    assert [r.card_number for r in rows] == ["1", "3"]


def test_read_queue_rejects_error_rows(tmp_path):
    path = tmp_path / "q.jsonl"
    path.write_text(
        json.dumps(ROW)
        + "\n"
        + json.dumps({"error": "No checklist match", "set_name": "S", "card_number": "9"})
        + "\n"
    )
    with pytest.raises(ValueError, match="enrichment error"):
        cl.read_queue(path)


def test_default_queue_source(tmp_path, monkeypatch):
    _point_settings_at(tmp_path, monkeypatch, exports_dir="exports")
    assert cl.default_queue_source(get_settings()) == Path("exports/jsonl") / cl.QUEUE_FILENAME

    _point_settings_at(
        tmp_path,
        monkeypatch,
        exports_dir="exports",
        queue_uri="gs://b/q/listing_queue_enriched.jsonl",
    )
    assert cl.default_queue_source(get_settings()) == "gs://b/q/listing_queue_enriched.jsonl"


class _Stub:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        return lambda *a, **k: None


def _stub_clients(monkeypatch):
    for name in ("ImageLogClient", "PriceScraper", "ListingService", "EbayClient"):
        monkeypatch.setattr(cl, name, _Stub)


def test_run_listings_queue_override_beats_setting(tmp_path, monkeypatch):
    _point_settings_at(
        tmp_path,
        monkeypatch,
        exports_dir=str(tmp_path),
        queue_uri="gs://b/q/listing_queue_enriched.jsonl",
    )
    _stub_clients(monkeypatch)
    sources = []

    def fake_read_queue(source):
        sources.append(source)
        return []

    monkeypatch.setattr(cl, "read_queue", fake_read_queue)
    cl.run_listings(queue="local/override.jsonl", dry_run=True)
    assert sources == ["local/override.jsonl"]


def test_run_listings_uses_setting_when_no_override(tmp_path, monkeypatch):
    _point_settings_at(
        tmp_path,
        monkeypatch,
        exports_dir=str(tmp_path),
        queue_uri="gs://b/q/listing_queue_enriched.jsonl",
    )
    _stub_clients(monkeypatch)
    sources = []
    monkeypatch.setattr(cl, "read_queue", lambda s: sources.append(s) or [])
    cl.run_listings(dry_run=True)
    assert sources == ["gs://b/q/listing_queue_enriched.jsonl"]


class _FakeImageLog:
    """Records the files create-listings hands it and deletes uploaded ones,
    like the real client (delete_original=True)."""

    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def upload_combined_image(self, *, front, back, **kwargs):
        self.calls.append(("combined", (front, back)))
        assert front.exists() and back.exists()
        return SimpleNamespace(public_url="https://gcs/combined.jpg")

    def upload_image(self, *, file_path, side, delete_original=True, **kwargs):
        self.calls.append((side, file_path))
        if delete_original:
            file_path.unlink()
        return SimpleNamespace(public_url=f"https://gcs/{side}.jpg")


def test_combined_image_uses_materialised_scan_number_and_gs_photo(tmp_path, monkeypatch):
    """A phone-built row: scan number front, gs:// photo back. Both are resolved
    to local files once, the composite is built from those files before the
    uploads delete them, and the pictures come back front, back, combined."""
    scans = tmp_path / "scans"
    _point_settings_at(tmp_path, monkeypatch, scans_dir=str(scans))
    (scans).mkdir()
    (scans / "SCAN_0123.jpg").write_bytes(b"front")

    downloads = []

    def fake_download(uri, dest):
        downloads.append(uri)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"back")
        return dest

    monkeypatch.setattr("shoebox.utils.image_refs.download_gs_file", fake_download)

    row = ListingQueueRow(
        set_year="2025",
        set_name="Topps",
        subset_name="Base",
        subset_type="Base",
        card_number="12",
        player="Barry Bonds",
        price=4.99,
        image_front="123",
        image_back="gs://img-bucket/uploads/2026-10/abc12345.jpg",
    )
    img = _FakeImageLog()
    result = cl._create_one_listing(
        row,
        sku="SKU1",
        quick_title="t",
        listings=None,
        img_client=img,
        price_scrape_client=None,
        publish=False,
        dry_run=True,
        schedule=False,
        scrape_prices=False,
    )

    front = scans / "SCAN_0123.jpg"
    back = scans / "app_uploads" / "abc12345.jpg"
    assert downloads == ["gs://img-bucket/uploads/2026-10/abc12345.jpg"]
    assert sorted(img.calls, key=lambda c: c[0]) == [
        ("back", back),
        ("combined", (front, back)),
        ("front", front),
    ]
    assert result.request.data["inventory_item"]["product"]["imageUrls"] == [
        "https://gcs/front.jpg",
        "https://gcs/back.jpg",
        "https://gcs/combined.jpg",
    ]
    assert not front.exists() and not back.exists()


def test_onedrive_scan_is_archived_after_its_upload(tmp_path, monkeypatch):
    """In the cloud the front is fetched from OneDrive; once uploaded it is moved
    into the Listed subfolder. The back is already on disk and is left alone."""
    scans = tmp_path / "scans"
    _point_settings_at(tmp_path, monkeypatch, scans_dir=str(scans))
    settings = get_settings()
    settings.onedrive.client_id = "app"
    settings.onedrive.scans_folder = "Scans"
    scans.mkdir()
    (scans / "SCAN_0124.jpg").write_bytes(b"back")

    events = []

    class FakeOneDrive:
        def download(self, name, dest):
            events.append(("download", name))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"front")
            return dest

        def move(self, name, subfolder):
            events.append(("move", name, subfolder))
            return f"Scans/{subfolder}/{name}"

    fake = FakeOneDrive()
    monkeypatch.setattr("shoebox.utils.image_refs._default_onedrive", lambda s: fake)

    class ImageLog(_FakeImageLog):
        def upload_image(self, **kwargs):
            events.append(("upload", kwargs["file_path"].name))
            return super().upload_image(**kwargs)

    row = ListingQueueRow(
        set_year="2025",
        set_name="Topps",
        subset_name="Base",
        subset_type="Base",
        card_number="12",
        player="P",
        image_front="123",
        image_back="124",
    )
    cl._create_one_listing(
        row,
        sku="SKU1",
        quick_title="t",
        listings=None,
        img_client=ImageLog(),
        price_scrape_client=None,
        publish=False,
        dry_run=True,
        schedule=False,
        scrape_prices=False,
    )
    # The two uploads run at once; only the OneDrive scan is moved, after both.
    assert events[0] == ("download", "SCAN_0123.jpg")
    assert sorted(events[1:3]) == [("upload", "SCAN_0123.jpg"), ("upload", "SCAN_0124.jpg")]
    assert events[3:] == [("move", "SCAN_0123.jpg", "Listed")]


def test_missing_gs_photo_fails_the_row_before_anything_uploads(tmp_path, monkeypatch):
    _point_settings_at(tmp_path, monkeypatch, scans_dir=str(tmp_path / "scans"))

    def failing(uri, dest):
        raise FileNotFoundError(f"Missing object: {uri}")

    monkeypatch.setattr("shoebox.utils.image_refs.download_gs_file", failing)
    row = ListingQueueRow(
        set_year="2025",
        set_name="Topps",
        subset_name="Base",
        subset_type="Base",
        card_number="12",
        player="P",
        image_front="gs://img-bucket/uploads/2026-10/missing.jpg",
        image_back="",
    )
    img = _FakeImageLog()
    with pytest.raises(FileNotFoundError):
        cl._create_one_listing(
            row,
            sku="SKU1",
            quick_title="t",
            listings=None,
            img_client=img,
            price_scrape_client=None,
            publish=False,
            dry_run=True,
            schedule=False,
            scrape_prices=False,
        )
    assert img.calls == []


# ---- summary, reporter, run-request mode ----


class _Reporter:
    def __init__(self):
        self.events = []

    def start(self, total):
        self.events.append(("start", total))

    def row(self, **kw):
        self.events.append(("row", kw))


def _stub_run_env(monkeypatch, tmp_path, outcomes):
    """Stub everything run_listings touches; outcomes maps card_number -> result or Exception."""
    _point_settings_at(tmp_path, monkeypatch, exports_dir=str(tmp_path / "exports"))
    _stub_clients(monkeypatch)
    synced = []
    monkeypatch.setattr(cl, "sync_results_to_bq", lambda **kw: synced.append(kw) or "obj")

    def fake_one(q, **kw):
        out = outcomes[q.card_number]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(cl, "_create_one_listing", fake_one)
    return synced


def _row(card):
    return ListingQueueRow(
        set_year="2025",
        set_name="Topps",
        subset_name="Base",
        subset_type="Base",
        card_number=card,
        player="P",
        quantity=1,
        price=1.0,
    )


def test_run_listings_returns_summary_and_reports_rows(tmp_path, monkeypatch):
    from shoebox.models.ebay_listing import EbayListingResult

    ok = EbayListingResult(sku="SKU-1", listing_id="L1", offer_id="O1")
    synced = _stub_run_env(monkeypatch, tmp_path, {"1": ok, "2": RuntimeError("ebay said no")})
    rep = _Reporter()
    summary = cl.run_listings(queue_items=[_row("1"), _row("2")], dry_run=True, reporter=rep)
    assert (summary.listed, summary.failed) == (1, 1)
    assert summary.results == [ok]
    assert summary.failures[0]["error"] == "ebay said no"
    assert rep.events[0] == ("start", 2)
    assert rep.events[1][1]["success"] is True and rep.events[1][1]["listing_id"] == "L1"
    assert rep.events[2][1]["success"] is False and "ebay said no" in rep.events[2][1]["error"]
    assert len(synced) == 1  # results file existed -> synced


def test_run_listings_syncs_in_finally(tmp_path, monkeypatch):
    from shoebox.models.ebay_listing import EbayListingResult

    ok = EbayListingResult(sku="SKU-1")
    synced = _stub_run_env(monkeypatch, tmp_path, {"1": ok})

    class Boom(BaseException):
        pass

    class Rep(_Reporter):
        def row(self, **kw):
            raise Boom()

    with pytest.raises(Boom):
        cl.run_listings(queue_items=[_row("1")], dry_run=True, reporter=Rep())
    assert len(synced) == 1


class _FakeStore:
    uri = "gs://b/queue/runs/"

    def __init__(self, run, generation=7, claim_ok=True):
        self.run = run
        self.generation = generation
        self.claim_ok = claim_ok
        self.writes = []

    def active(self, now=None):
        return self.run

    def read(self, run_id):
        return (self.run, self.generation) if self.run and self.run.run_id == run_id else None

    def claim(self, run, generation, now=None):
        assert generation == self.generation
        if self.claim_ok:
            run.state = "running"
        return self.claim_ok

    def write(self, run, *, if_generation_match=None):
        self.writes.append(run.model_copy(deep=True))
        return 1


def _request_run(**over):
    from shoebox.services.listing_runs import ListingRun

    base = dict(
        run_id="20261015T120000Z-aaaa0000",
        requested_at="2026-10-15T12:00:00+00:00",
        publish=False,
        schedule=True,
        queue_uri="gs://b/queue/listing_queue_enriched.jsonl",
        rows_total=1,
    )
    base.update(over)
    return ListingRun(**base)


def test_run_listing_request_runs_with_record_flags(tmp_path, monkeypatch):
    _point_settings_at(tmp_path, monkeypatch, queue_uri="gs://b/queue/listing_queue_enriched.jsonl")
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    calls = []
    notes = []
    monkeypatch.setattr(cl, "notify_best_effort", lambda ch, msg: notes.append(msg))

    def fake_run_listings(**kw):
        calls.append(kw)
        kw["reporter"].row(sku="S", title="t", success=True, listing_id="L")
        return cl.RunSummary(listed=1)

    monkeypatch.setattr(cl, "run_listings", fake_run_listings)
    store = _FakeStore(_request_run())
    assert cl.run_listing_request(store=store) == 0
    assert calls[0]["queue"] == "gs://b/queue/listing_queue_enriched.jsonl"
    assert calls[0]["publish"] is False and calls[0]["schedule"] is True
    assert calls[0]["scrape_prices"] is False and calls[0]["dry_run"] is False
    final = store.writes[-1]
    assert final.state == "done" and final.listed == 1 and final.results[0].listing_id == "L"
    assert notes[0].startswith("Starting create-listings (app run") and "publish=off" in notes[0]
    assert "1 listed, 0 failed" in notes[-1]


def test_run_listing_request_nothing_to_do(tmp_path, monkeypatch):
    _point_settings_at(tmp_path, monkeypatch, queue_uri="gs://b/queue/listing_queue_enriched.jsonl")
    monkeypatch.setattr(cl, "run_listings", lambda **kw: pytest.fail("must not run"))
    assert cl.run_listing_request(store=_FakeStore(None)) == 0
    assert cl.run_listing_request(store=_FakeStore(_request_run(state="running"))) == 0
    assert cl.run_listing_request(store=_FakeStore(_request_run(), claim_ok=False)) == 0


def test_run_listing_request_failure_marks_record_and_reraises(tmp_path, monkeypatch):
    _point_settings_at(tmp_path, monkeypatch, queue_uri="gs://b/queue/listing_queue_enriched.jsonl")
    notes = []
    monkeypatch.setattr(cl, "notify_best_effort", lambda ch, msg: notes.append(msg))

    def boom(**kw):
        raise RuntimeError("queue missing")

    monkeypatch.setattr(cl, "run_listings", boom)
    store = _FakeStore(_request_run())
    with pytest.raises(RuntimeError, match="queue missing"):
        cl.run_listing_request(store=store)
    assert store.writes[-1].state == "failed" and "queue missing" in store.writes[-1].error
    assert "failed" in notes[-1]


def test_run_listing_request_guards(tmp_path, monkeypatch):
    _point_settings_at(tmp_path, monkeypatch, exports_dir="exports")
    with pytest.raises(RuntimeError, match="queue_uri"):
        cl.run_listing_request(store=_FakeStore(None))

    _point_settings_at(tmp_path, monkeypatch, queue_uri="gs://b/q/listing_queue_enriched.jsonl")
    monkeypatch.setenv("CLOUD_RUN_JOB", "create-listings")
    settings = get_settings()
    settings.onedrive.client_id = "x"
    settings.onedrive.scans_folder = "Scans"
    with pytest.raises(RuntimeError, match="token_cache_uri"):
        cl.run_listing_request(store=_FakeStore(None), settings=settings)


# ---- prefetch, background archive, early done, scans folder ----


class _FakeOneDrive:
    def __init__(self, missing=()):
        self.missing = set(missing)
        self.downloads: list[str] = []
        self.moves: list[str] = []

    def download(self, name, dest):
        self.downloads.append(name)
        if name in self.missing:
            raise FileNotFoundError(f"OneDrive: {name} not found")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"scan")
        return dest

    def move(self, name, subfolder):
        self.moves.append(name)
        return f"Scans/{subfolder}/{name}"


def _onedrive_run_env(tmp_path, monkeypatch, onedrive):
    _point_settings_at(
        tmp_path,
        monkeypatch,
        scans_dir=str(tmp_path / "scans"),
        exports_dir=str(tmp_path / "exports"),
    )
    settings = get_settings()
    settings.onedrive.client_id = "app"
    settings.onedrive.scans_folder = "Scans"
    _stub_clients(monkeypatch)
    img = _FakeImageLog()
    img.flush_append_log = lambda: None
    monkeypatch.setattr(cl, "ImageLogClient", lambda **kw: img)
    monkeypatch.setattr("shoebox.utils.image_refs._default_onedrive", lambda s: onedrive)
    synced = []
    monkeypatch.setattr(cl, "sync_results_to_bq", lambda **kw: synced.append(kw) or "obj")
    return settings, img, synced


def _scan_row(card, front, back=""):
    row = _row(card)
    row.image_front = front
    row.image_back = back
    return row


def test_run_listings_prefetches_scans_and_archives_them(tmp_path, monkeypatch):
    onedrive = _FakeOneDrive(missing={"SCAN_0009.jpg"})
    settings, img, synced = _onedrive_run_env(tmp_path, monkeypatch, onedrive)
    rows = [
        _scan_row("1", "1", "2"),
        _scan_row("2", "9"),  # missing in OneDrive: only this row fails
        _scan_row("3", "3", "4"),
    ]
    order = []
    rep = _Reporter()
    summary = cl.run_listings(
        queue_items=rows,
        dry_run=True,
        reporter=rep,
        settings=settings,
        on_complete=lambda s: order.append(("complete", len(onedrive.moves), len(synced))),
    )
    assert (summary.listed, summary.failed) == (2, 1)
    assert "SCAN_0009.jpg" in rep.events[2][1]["error"]
    assert sorted(onedrive.downloads) == [
        "SCAN_0001.jpg",
        "SCAN_0002.jpg",
        "SCAN_0003.jpg",
        "SCAN_0004.jpg",
        "SCAN_0009.jpg",
    ]
    assert sorted(onedrive.moves) == [
        "SCAN_0001.jpg",
        "SCAN_0002.jpg",
        "SCAN_0003.jpg",
        "SCAN_0004.jpg",
    ]
    # Done is reported once every move finished, before the results sync.
    assert order == [("complete", 4, 0)]
    assert len(synced) == 1
    assert not list((tmp_path / "scans").glob("SCAN_*.jpg"))


def test_prefetch_downloads_a_shared_scan_once(tmp_path, monkeypatch):
    onedrive = _FakeOneDrive()
    settings, img, _ = _onedrive_run_env(tmp_path, monkeypatch, onedrive)
    pool = cl.ThreadPoolExecutor(4)
    try:
        prefetch = cl._ImagePrefetch(
            [_scan_row("1", "5"), _scan_row("2", " 5 ")], settings=settings, pool=pool
        )
        first, _ = prefetch.get(0)
        second, back = prefetch.get(1)
    finally:
        pool.shutdown()
    assert onedrive.downloads == ["SCAN_0005.jpg"]
    assert first is second and back is None


def test_prefetch_stays_a_window_ahead(tmp_path, monkeypatch):
    onedrive = _FakeOneDrive()
    settings, *_ = _onedrive_run_env(tmp_path, monkeypatch, onedrive)
    rows = [_scan_row(str(i), str(i)) for i in range(1, 31)]
    pool = cl.ThreadPoolExecutor(4)
    try:
        prefetch = cl._ImagePrefetch(rows, settings=settings, pool=pool)
        prefetch.ensure(1 + cl.PREFETCH_AHEAD)
        prefetch.get(0)
    finally:
        pool.shutdown()
    assert len(onedrive.downloads) == 1 + cl.PREFETCH_AHEAD


def test_run_listing_request_uses_requested_scans_folder(tmp_path, monkeypatch):
    _point_settings_at(tmp_path, monkeypatch, queue_uri="gs://b/queue/listing_queue_enriched.jsonl")
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    settings = get_settings()
    settings.onedrive.client_id = "app"
    settings.onedrive.scans_folder = "Scans"
    calls, notes = [], []
    monkeypatch.setattr(cl, "notify_best_effort", lambda ch, msg: notes.append(msg))
    monkeypatch.setattr(cl, "run_listings", lambda **kw: calls.append(kw) or cl.RunSummary())
    store = _FakeStore(_request_run(scans_folder="Pictures/Batch 7"))
    assert cl.run_listing_request(store=store, settings=settings) == 0
    assert calls[0]["settings"].onedrive.scans_folder == "Pictures/Batch 7"
    assert settings.onedrive.scans_folder == "Scans"  # the shared settings are untouched
    assert "scans from Pictures/Batch 7" in notes[0]
    assert store.writes[-1].state == "done"


def test_run_listing_request_marks_done_before_sync_and_keeps_it(tmp_path, monkeypatch):
    """The record goes to done from on_complete; a results-sync failure after
    that re-raises (job alert) without flipping the record to failed."""
    _point_settings_at(tmp_path, monkeypatch, queue_uri="gs://b/queue/listing_queue_enriched.jsonl")
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    notes = []
    monkeypatch.setattr(cl, "notify_best_effort", lambda ch, msg: notes.append(msg))

    def fake_run_listings(**kw):
        kw["on_complete"](cl.RunSummary(listed=1))
        raise RuntimeError("bigquery load failed")

    monkeypatch.setattr(cl, "run_listings", fake_run_listings)
    store = _FakeStore(_request_run())
    with pytest.raises(RuntimeError, match="bigquery"):
        cl.run_listing_request(store=store)
    assert [w.state for w in store.writes] == ["done"]
    assert "1 listed, 0 failed" in notes[1]
    assert "results sync failed" in notes[-1]
