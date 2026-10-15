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

    def upload_image(self, *, file_path, side, **kwargs):
        self.calls.append((side, file_path))
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
    assert img.calls == [("combined", (front, back)), ("front", front), ("back", back)]
    assert result.request.data["inventory_item"]["product"]["imageUrls"] == [
        "https://gcs/front.jpg",
        "https://gcs/back.jpg",
        "https://gcs/combined.jpg",
    ]
    assert not front.exists() and not back.exists()


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
