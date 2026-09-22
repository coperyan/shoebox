import json
from pathlib import Path

import pytest

from shoebox.clients.ebay.trading import TradingQuotaExceeded
from shoebox.pipelines import sync_watch_list as mod

WATCHED = [
    {
        "item_id": "111",
        "title": "2025 Topps Chrome Ohtani #1",
        "seller": "cardshop",
        "listing_type": "Chinese",
        "listing_status": "Active",
        "price": "25.50",
        "currency": "USD",
        "buy_it_now_price": "0.0",
        "bid_count": "4",
        "quantity": "1",
        "time_left": "P1DT2H",
        "start_time": "2026-09-15T18:00:00.000Z",
        "end_time": "2026-09-22T18:00:00.000Z",
        "view_item_url": "https://www.ebay.com/itm/111",
        "gallery_url": "https://i.ebayimg.com/111.jpg",
    },
    {
        "item_id": "222",
        "title": "Ended listing",
        "seller": "other",
        "price": None,
        "start_time": None,
        "end_time": "2026-09-01T00:00:00.000Z",
    },
]

DETAILS = [
    {
        "item_id": "111",
        "price": "25.50",
        "quantity_sold": "0",
        "category_id": "261328",
        "category_name": "Sports Trading Cards",
        "condition_id": "4000",
        "condition_display_name": "Ungraded",
        "item_specifics": {"Player/Athlete": ["Shohei Ohtani"], "Team": ["LAD"]},
        "picture_urls": ["https://i.ebayimg.com/111-1.jpg"],
    }
]


def _schema_names() -> list[str]:
    raw = json.loads(Path("configs/bigquery/schemas/watch_list.json").read_text("utf-8"))
    return [f["name"] for f in raw]


class TestBuildRows:
    def test_rows_match_schema_exactly(self):
        # load_jsonl_from_gcs does not ignore unknown fields, so a key the
        # schema lacks would fail the whole load.
        rows = mod.build_watch_list_rows(WATCHED, DETAILS, {}, file_date="2026-09-22T00:00:00Z")
        for row in rows:
            assert list(row) == _schema_names()

    def test_merges_watch_list_and_details(self):
        (row, _) = mod.build_watch_list_rows(
            WATCHED, DETAILS, {"222": "ended"}, file_date="2026-09-22T00:00:00Z"
        )
        assert row["price"] == 25.5
        assert row["bid_count"] == 4
        assert row["quantity_sold"] == 0
        assert row["seller"] == "cardshop"
        assert row["category_name"] == "Sports Trading Cards"
        assert row["item_specifics"] == {"Player/Athlete": ["Shohei Ohtani"], "Team": ["LAD"]}
        assert row["start_time"] == "2026-09-15T18:00:00Z"
        assert row["end_time"] == "2026-09-22T18:00:00Z"
        assert row["detail_error"] is None

    def test_failed_detail_keeps_watch_list_fields(self):
        (_, row) = mod.build_watch_list_rows(
            WATCHED, DETAILS, {"222": "ended"}, file_date="2026-09-22T00:00:00Z"
        )
        assert row["title"] == "Ended listing"
        assert row["item_specifics"] is None
        assert row["price"] is None
        assert row["start_time"] is None
        assert row["detail_error"] == "ended"


class TestPipeline:
    def _patch(self, monkeypatch, tmp_path, trading):
        class FakeEbay:
            pass

        FakeEbay.trading = trading
        loads, uploads = [], []

        class FakeGCS:
            def upload_text(self, **kw):
                uploads.append(kw)

        class FakeBQ:
            def load_jsonl_from_gcs(self, **kw):
                loads.append(kw)

        class Settings:
            class paths:
                exports_dir = str(tmp_path)

            class gcs:
                ebay_bucket = "bucket"

            class bigquery:
                ebay_dataset = "ebay"

            class slack:
                notify_channel = "c"

        monkeypatch.setattr(mod, "EbayClient", lambda: FakeEbay())
        monkeypatch.setattr(mod, "GCSClient", lambda: FakeGCS())
        monkeypatch.setattr(mod, "BigQueryClient", lambda: FakeBQ())
        monkeypatch.setattr(mod, "get_settings", lambda: Settings)
        monkeypatch.setattr(mod, "notify_best_effort", lambda *a, **kw: None)
        return uploads, loads

    def test_writes_uploads_and_loads(self, monkeypatch, tmp_path):
        class FakeTrading:
            def get_watch_list(self):
                return WATCHED

            def get_item_details_bulk(self, item_ids, **kw):
                assert item_ids == ["111", "222"]
                return DETAILS, {"222": "ended"}

        uploads, loads = self._patch(monkeypatch, tmp_path, FakeTrading())
        mod.sync_watch_list()

        written = (tmp_path / "jsonl/watch_list.jsonl").read_text("utf-8").splitlines()
        assert len(written) == 2
        (upload,) = uploads
        (load,) = loads
        assert upload["object_name"].startswith("logs/watch_list/watch_list_")
        assert load["object_name"] == upload["object_name"]
        assert load["table"] == "watch_list"
        assert load["write_disposition"] == "WRITE_APPEND"

    def test_quota_exhaustion_stops_before_writing(self, monkeypatch, tmp_path):
        class FakeTrading:
            def get_watch_list(self):
                return WATCHED

            def get_item_details_bulk(self, item_ids, **kw):
                raise TradingQuotaExceeded("allowance exhausted")

        uploads, loads = self._patch(monkeypatch, tmp_path, FakeTrading())
        with pytest.raises(TradingQuotaExceeded):
            mod.sync_watch_list()
        assert uploads == [] and loads == []
        assert not (tmp_path / "jsonl/watch_list.jsonl").exists()
