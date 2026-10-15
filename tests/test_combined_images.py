"""Tests for the combined front+back picture: composing it, attaching it to new
listings, keeping relist from clobbering it, and backfilling live listings.

eBay, GCS and HTTP are replaced with plain local fakes throughout.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from PIL import Image

from shoebox.utils.images import COMBINED_GAP, COMBINED_PADDING, combine_front_back


def _jpeg(path: Path | None, size: tuple[int, int], color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    if path is not None:
        path.write_bytes(buf.getvalue())
    return buf.getvalue()


class TestCombineFrontBack:
    def test_side_by_side_at_matched_height(self):
        out = Image.open(
            io.BytesIO(combine_front_back(_jpeg(None, (100, 140)), _jpeg(None, (50, 70))))
        )

        assert out.format == "JPEG"
        # The back is scaled up to the front's height, then both sit in a row.
        assert out.height == 140 + 2 * COMBINED_PADDING
        assert out.width == 100 + 100 + COMBINED_GAP + 2 * COMBINED_PADDING

    def test_large_scans_are_capped_in_width(self):
        out = combine_front_back(
            _jpeg(None, (2000, 2800)), _jpeg(None, (2000, 2800)), max_width=1000
        )
        assert Image.open(io.BytesIO(out)).width == 1000

    def test_reads_files_as_well_as_bytes(self, tmp_path):
        front, back = tmp_path / "f.jpg", tmp_path / "b.jpg"
        _jpeg(front, (10, 14))
        _jpeg(back, (10, 14))
        assert Image.open(io.BytesIO(combine_front_back(front, back))).width > 20


class TestUploadCombinedImage:
    def _client(self, cached: dict | None = None):
        from shoebox.clients.image_log import ImageLogClient

        # Skip __init__: it builds real GCS/BigQuery clients.
        client = ImageLogClient.__new__(ImageLogClient)
        client.calls = []
        cached = cached or {}

        def upload_image(*, file_path, side, **kwargs):
            if file_path.name in cached:
                return cached[file_path.name]
            # The composite exists on disk by the time it is uploaded.
            client.calls.append((file_path.name, side, file_path.read_bytes()[:2]))
            return SimpleNamespace(public_url=f"https://gcs/{side}/{file_path.name}")

        client.upload_image = upload_image
        return client

    def _meta(self):
        return {"set_name": "Topps", "subset_name": "Base", "card_number": "12"}

    def test_builds_and_uploads_composite_without_touching_the_scans(self, tmp_path):
        front, back = tmp_path / "SCAN_0001.jpg", tmp_path / "SCAN_0002.jpg"
        _jpeg(front, (10, 14))
        _jpeg(back, (10, 14))
        client = self._client()

        entry = client.upload_combined_image(front=front, back=back, **self._meta())

        assert entry.public_url == "https://gcs/combined/SCAN_0001_SCAN_0002.jpg"
        assert client.calls == [("SCAN_0001_SCAN_0002.jpg", "combined", b"\xff\xd8")]
        assert front.exists() and back.exists()

    def test_rerun_after_scans_were_deleted_is_answered_from_cache(self, tmp_path):
        cached = SimpleNamespace(public_url="https://gcs/cached.jpg")
        client = self._client({"SCAN_0001_SCAN_0002.jpg": cached})

        entry = client.upload_combined_image(
            front=tmp_path / "SCAN_0001.jpg", back=tmp_path / "SCAN_0002.jpg", **self._meta()
        )

        assert entry is cached


class FakeImageLog:
    """Mimics create-listings' use of ImageLogClient, including deleting scans."""

    def __init__(self, combined_fails: bool = False):
        self.combined_fails = combined_fails
        self.order: list[str] = []

    def upload_combined_image(self, *, front, back, **kwargs):
        self.order.append("combined")
        if self.combined_fails or not (front.exists() and back.exists()):
            raise FileNotFoundError("scans already gone")
        return SimpleNamespace(public_url="https://gcs/combined.jpg")

    def upload_image(self, *, file_path, side, **kwargs):
        self.order.append(side)
        file_path.unlink()  # delete_original=True
        return SimpleNamespace(public_url=f"https://gcs/{side}.jpg")


class TestCreateListingImages:
    def _row(self, tmp_path):
        from shoebox.models.listing_queue import ListingQueueRow

        front, back = tmp_path / "front.jpg", tmp_path / "back.jpg"
        _jpeg(front, (10, 14))
        _jpeg(back, (10, 14))
        return ListingQueueRow(
            set_year="2025",
            set_name="Topps",
            subset_name="Base",
            subset_type="Base",
            card_number="12",
            player="Barry Bonds",
            team="San Francisco Giants",
            price=4.99,
            image_front=str(front),
            image_back=str(back),
        )

    def _image_urls(self, row, img_client):
        from shoebox.pipelines.create_listings import _create_one_listing

        result = _create_one_listing(
            row,
            sku="SKU1",
            quick_title="t",
            listings=None,
            img_client=img_client,
            price_scrape_client=None,
            publish=False,
            dry_run=True,
            schedule=False,
            scrape_prices=False,
        )
        return result.request.data["inventory_item"]["product"]["imageUrls"]

    def test_combined_is_built_before_scans_are_deleted_and_listed_third(self, tmp_path):
        img = FakeImageLog()
        urls = self._image_urls(self._row(tmp_path), img)

        assert img.order == ["combined", "front", "back"]
        assert urls == [
            "https://gcs/front.jpg",
            "https://gcs/back.jpg",
            "https://gcs/combined.jpg",
        ]

    def test_listing_goes_ahead_without_combined_when_it_fails(self, tmp_path):
        urls = self._image_urls(self._row(tmp_path), FakeImageLog(combined_fails=True))
        assert urls == ["https://gcs/front.jpg", "https://gcs/back.jpg"]

    def test_front_only_row_gets_no_combined(self, tmp_path):
        row = self._row(tmp_path)
        row.image_back = ""
        img = FakeImageLog()

        assert self._image_urls(row, img) == ["https://gcs/front.jpg"]
        assert img.order == ["front"]


class FakeBlob:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self.public_url = f"https://storage.googleapis.com/bucket/{name}"

    def upload_from_string(self, data, content_type=None):
        self.store[self.name] = data


class FakeGCS:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        bucket = SimpleNamespace(blob=lambda name: FakeBlob(self.objects, name))
        self.client = SimpleNamespace(bucket=lambda _name: bucket)


class TestRelistKeepsEveryPicture:
    def test_three_pictures_land_in_three_objects(self, monkeypatch):
        from shoebox.pipelines import relist_listings

        monkeypatch.setattr("requests.get", lambda url: SimpleNamespace(content=url.encode()))
        gcs = FakeGCS()
        settings = SimpleNamespace(gcs=SimpleNamespace(image_bucket="bucket"))

        urls = relist_listings.ebay_image_to_gcs(
            [
                "https://i.ebayimg.com/a/$_1.JPG",
                "https://i.ebayimg.com/b/$_1.JPG",
                "https://i.ebayimg.com/c/$_1.JPG",
            ],
            "SKU1",
            gcs,
            settings,
        )

        assert sorted(gcs.objects) == [
            "other/SKU1_back.png",
            "other/SKU1_combined.png",
            "other/SKU1_front.png",
        ]
        assert len(set(urls)) == 3
        assert gcs.objects["other/SKU1_combined.png"] == b"https://i.ebayimg.com/c/$_57.JPG"


def _inventory_item(image_urls):
    from shoebox.models.ebay.inventory_item import InventoryItem

    return InventoryItem.from_api(
        {
            "sku": "SKU1",
            "condition": "USED_VERY_GOOD",
            "availability": {"ship_to_location_availability": {"quantity": 1}},
            "product": {
                "title": "2025 Topps Barry Bonds #12",
                "description": "<div>Set: 2025 Topps</div>",
                "aspects": {"Card Number": "12"},
                "image_urls": image_urls,
            },
        }
    )


class FakeInventory:
    def __init__(self, image_urls):
        self.item = _inventory_item(image_urls)
        self.upserts: list[tuple[str, dict]] = []

    def get_inventory_item(self, sku):
        return self.item

    def upsert_inventory_item(self, sku, body):
        self.upserts.append((sku, body))


class TestAddCombinedImages:
    def test_plan_only_targets_two_picture_single_card_listings(self):
        from shoebox.pipelines.add_combined_images import build_image_plan

        df = pd.DataFrame(
            [
                {"item_id": 1.0, "sku": "A", "title": "Card A", "picture_urls": ["f", "b"]},
                {"item_id": 2, "sku": "B", "title": "Card B", "picture_urls": '["f", "b", "c"]'},
                {"item_id": 3, "sku": None, "title": "Card C", "picture_urls": ["f", "b"]},
                {
                    "item_id": 4,
                    "sku": "D",
                    "title": "Complete Your Set",
                    "picture_urls": ["f", "b"],
                },
                {"item_id": 5, "sku": "E", "title": "Card E", "picture_urls": float("nan")},
            ]
        )

        plan = build_image_plan(df)

        assert plan["eligible"].tolist() == [True, False, False, False, False]
        assert plan["skip_reason"].tolist() == [
            "",
            "has_3_pictures",
            "no_sku",
            "variation_listing",
            "has_0_pictures",
        ]
        assert plan["item_id"].iloc[0] == "1"

    def test_without_picture_urls_column_the_count_is_left_to_the_live_check(self):
        from shoebox.pipelines.add_combined_images import build_image_plan

        # v_active_listing_details has no picture_urls column.
        df = pd.DataFrame(
            [
                {"item_id": 1, "sku": "A", "title": "Card A"},
                {"item_id": 2, "sku": None, "title": "Card B"},
            ]
        )

        plan = build_image_plan(df)

        assert plan["eligible"].tolist() == [True, False]
        assert plan["skip_reason"].tolist() == ["", "no_sku"]
        assert plan["picture_count"].isna().all()

    def test_limit_counts_updates_not_listings_checked(self, monkeypatch):
        from shoebox.pipelines import add_combined_images as mod

        outcomes = {"A": "skipped", "B": "skipped", "C": "updated", "D": "restored", "E": "updated"}
        seen = []

        def fake_add(sku, item_id, **kwargs):
            seen.append(sku)
            return outcomes[sku]

        monkeypatch.setattr(mod, "add_combined_image", fake_add)
        plan = pd.DataFrame([{"sku": s, "item_id": s, "eligible": True} for s in outcomes])

        applied = mod.apply_combined_images(plan, ebay=object(), gcs=object(), limit=2)

        assert seen == ["A", "B", "C", "D"]
        assert applied["status"].tolist() == ["skipped", "skipped", "updated", "restored"]

    def _ebay(self, pictures, inventory_urls):
        trading = SimpleNamespace(get_item_details=lambda item_id: {"picture_urls": pictures})
        return SimpleNamespace(trading=trading, inventory=FakeInventory(inventory_urls))

    def _fake_http(self, monkeypatch):
        fetched = []

        def get(url, timeout=None):
            fetched.append(url)
            return SimpleNamespace(content=_jpeg(None, (10, 14)), raise_for_status=lambda: None)

        monkeypatch.setattr("requests.get", get)
        return fetched

    def _settings(self):
        return SimpleNamespace(gcs=SimpleNamespace(image_bucket="bucket"))

    def test_rehosts_live_pictures_and_sets_all_three(self, monkeypatch):
        from shoebox.pipelines import add_combined_images as mod

        fetched = self._fake_http(monkeypatch)
        # eBay shows two pictures, but the inventory item only lists the front;
        # writing that list back would drop the back.
        ebay = self._ebay(
            ["https://i.ebayimg.com/f/$_1.PNG", "https://i.ebayimg.com/b/$_1.PNG"],
            ["https://storage.googleapis.com/bucket/images/front__SCAN_1.png"],
        )
        gcs = FakeGCS()

        status = mod.add_combined_image(
            "SKU1", "111", ebay=ebay, gcs=gcs, settings=self._settings()
        )

        assert status == "updated"
        gcs_base = "https://storage.googleapis.com/bucket/other/SKU1"
        assert fetched == [
            "https://i.ebayimg.com/f/$_57.PNG",
            "https://i.ebayimg.com/b/$_57.PNG",
            f"{gcs_base}_front.png",
            f"{gcs_base}_back.png",
        ]
        assert sorted(gcs.objects) == [
            "other/SKU1_back.png",
            "other/SKU1_combined.jpg",
            "other/SKU1_front.png",
        ]
        ((sku, body),) = ebay.inventory.upserts
        assert sku == "SKU1"
        assert body["product"]["imageUrls"] == [
            f"{gcs_base}_front.png",
            f"{gcs_base}_back.png",
            f"{gcs_base}_combined.jpg",
        ]
        assert body["product"]["title"] == "2025 Topps Barry Bonds #12"
        assert body["product"]["description"] == "<div>Set: 2025 Topps</div>"
        assert body["condition"] == "USED_VERY_GOOD"

    def test_listings_without_exactly_two_live_pictures_are_left_alone(self, monkeypatch):
        from shoebox.pipelines import add_combined_images as mod

        fetched = self._fake_http(monkeypatch)
        for pictures in (["f"], ["f", "b", "combined"]):
            ebay = self._ebay(pictures, ["f"])
            status = mod.add_combined_image(
                "SKU1", "111", ebay=ebay, gcs=FakeGCS(), settings=self._settings()
            )
            assert status == "skipped"
            assert ebay.inventory.upserts == []
        assert fetched == []

    def test_restores_a_missing_back_from_gcs(self, monkeypatch):
        from shoebox.pipelines import add_combined_images as mod

        fetched = self._fake_http(monkeypatch)
        ebay = self._ebay(["https://i.ebayimg.com/f/$_1.PNG"], ["gcs/front"])
        gcs = FakeGCS()
        stored_back = "https://storage.googleapis.com/bucket/images/Topps/back__SCAN_2.png"

        status = mod.add_combined_image(
            "SKU1", "111", ebay=ebay, gcs=gcs, settings=self._settings(), back_url=stored_back
        )

        assert status == "restored"
        gcs_base = "https://storage.googleapis.com/bucket/other/SKU1"
        assert fetched == ["https://i.ebayimg.com/f/$_57.PNG", f"{gcs_base}_front.png", stored_back]
        # Only the front is re-hosted: the relist copy of the back is untouched.
        assert sorted(gcs.objects) == ["other/SKU1_combined.jpg", "other/SKU1_front.png"]
        ((_, body),) = ebay.inventory.upserts
        assert body["product"]["imageUrls"] == [
            f"{gcs_base}_front.png",
            stored_back,
            f"{gcs_base}_combined.jpg",
        ]

    def test_one_picture_listing_without_a_stored_back_is_left_alone(self, monkeypatch):
        from shoebox.pipelines import add_combined_images as mod

        self._fake_http(monkeypatch)
        ebay = self._ebay(["f"], ["f"])
        status = mod.add_combined_image(
            "SKU1", "111", ebay=ebay, gcs=FakeGCS(), settings=self._settings(), back_url=None
        )
        assert status == "skipped"
        assert ebay.inventory.upserts == []

    def test_plan_with_stored_backs_marks_restores(self):
        from shoebox.pipelines.add_combined_images import build_image_plan

        df = pd.DataFrame(
            [
                {"item_id": 1, "sku": "A", "title": "Card A", "picture_urls": ["f", "b"]},
                {"item_id": 2, "sku": "B", "title": "Card B", "picture_urls": ["f"]},
                {"item_id": 3, "sku": "C", "title": "Card C", "picture_urls": ["f"]},
            ]
        )

        plan = build_image_plan(df, {"B": "https://gcs/B_back.png"})

        assert plan["action"].tolist() == ["combine", "restore_back", ""]
        assert plan["skip_reason"].tolist() == ["", "", "no_stored_back"]
        assert plan["back_url"].tolist() == ["", "https://gcs/B_back.png", ""]

    def test_stored_backs_prefer_the_creation_scan_and_must_exist(self):
        from shoebox.pipelines.add_combined_images import find_stored_backs

        base = "https://storage.googleapis.com/bucket/"
        created = pd.DataFrame(
            [
                {"sku": "A", "back_url": f"{base}images/A/back__SCAN_2.png"},
                {"sku": "B", "back_url": f"{base}images/B/back__SCAN_4.png"},  # deleted
                {"sku": "C", "back_url": None},
            ]
        )
        names = [
            "images/A/back__SCAN_2.png",
            "other/A_back.png",
            "other/B_back.png",
            "other/C_back.png",
        ]
        bq = SimpleNamespace(run_query=lambda sql, params: created)
        gcs = SimpleNamespace(
            client=SimpleNamespace(
                list_blobs=lambda bucket: [SimpleNamespace(name=n) for n in names]
            )
        )
        settings = SimpleNamespace(
            gcs=SimpleNamespace(image_bucket="bucket"),
            bigquery=SimpleNamespace(ebay_dataset="ebay"),
        )

        backs = find_stored_backs(["A", "B", "C", "D"], bq=bq, gcs=gcs, settings=settings)

        assert backs == {
            "A": f"{base}images/A/back__SCAN_2.png",
            "B": f"{base}other/B_back.png",
            "C": f"{base}other/C_back.png",
        }

    def test_apply_records_failures_and_continues(self, monkeypatch):
        from shoebox.pipelines import add_combined_images as mod

        def fake_add(sku, item_id, **kwargs):
            if sku == "BAD":
                raise RuntimeError("boom")
            return "updated"

        monkeypatch.setattr(mod, "add_combined_image", fake_add)
        plan = pd.DataFrame(
            [
                {"sku": "A", "item_id": "1", "eligible": True},
                {"sku": "BAD", "item_id": "2", "eligible": True},
                {"sku": "C", "item_id": "3", "eligible": False},
                {"sku": "D", "item_id": "4", "eligible": True},
            ]
        )

        applied = mod.apply_combined_images(plan, ebay=object(), gcs=object())

        assert applied["sku"].tolist() == ["A", "BAD", "D"]
        assert applied["status"].tolist() == ["updated", "failed", "updated"]
        assert applied["error"].tolist() == ["", "boom", ""]
