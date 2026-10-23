"""refresh-listings: exact round-trip bodies, the delete-then-recreate order, recovery."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from shoebox.clients.ebay.errors import EbayClientError
from shoebox.models.ebay.inventory_item import InventoryItem
from shoebox.models.ebay.offer import Offer
from shoebox.pipelines import refresh_listings
from shoebox.services.listings import ListingService
from shoebox.transforms.listing_builder import refresh_inventory_item_body, round_trip_offer_body

OFFER_RAW = {
    "offer_id": "111",
    "sku": "SKU1",
    "status": "PUBLISHED",
    "listing": {"listing_id": "999", "listing_status": "ACTIVE"},
    "listing_start_date": "2026-01-01T00:00:00Z",
    "marketplace_id": "EBAY_US",
    "format": "FIXED_PRICE",
    "available_quantity": 1,
    "category_id": "261328",
    "secondary_category_id": None,
    "listing_description": "<p>desc</p>",
    "listing_duration": "GTC",
    "merchant_location_key": "home",
    "listing_policies": {
        "fulfillment_policy_id": "F",
        "payment_policy_id": "P",
        "return_policy_id": "R",
        "best_offer_terms": {
            "best_offer_enabled": True,
            "auto_decline_price": {"value": "3.00", "currency": "USD"},
        },
        "e_bay_plus_if_eligible": False,
    },
    "pricing_summary": {"price": {"value": "4.99", "currency": "USD"}},
    "store_category_names": ["/Baseball/Base", "/None"],
    "tax": {"apply_tax": False},
    "include_catalog_product_details": True,
}

ITEM_RAW = {
    "sku": "SKU1",
    "availability": {"ship_to_location_availability": {"quantity": 1}},
    "condition": "USED_VERY_GOOD",
    "condition_description": "Light corner wear",
    "condition_descriptors": [{"name": "40001", "values": ["400010"]}],
    "package_weight_and_size": {
        "dimensions": {"height": 1, "length": 7, "width": 5, "unit": "INCH"},
        "package_type": "LETTER",
        "weight": {"unit": "OUNCE", "value": 1},
    },
    "product": {
        "title": "2026 Topps Card",
        "description": "<p>item desc</p>",
        "aspects": {"Player/Athlete": ["A Player"], "Features": ["Rookie", "SP"]},
        "image_urls": ["https://i.ebayimg.com/front.jpg"],
        "brand": "Topps",
    },
}


class TestRoundTripOfferBody:
    def test_reproduces_the_offer_without_state_fields(self):
        body = round_trip_offer_body(OFFER_RAW)
        for key in ("offerId", "status", "listing", "listingStartDate", "secondaryCategoryId"):
            assert key not in body
        assert body["sku"] == "SKU1"
        assert body["merchantLocationKey"] == "home"
        assert body["pricingSummary"] == {"price": {"value": "4.99", "currency": "USD"}}
        assert body["listingPolicies"]["bestOfferTerms"] == {
            "bestOfferEnabled": True,
            "autoDeclinePrice": {"value": "3.00", "currency": "USD"},
        }
        assert body["listingPolicies"]["eBayPlusIfEligible"] is False
        assert body["tax"] == {"applyTax": False}
        assert body["includeCatalogProductDetails"] is True
        assert body["storeCategoryNames"] == ["/Baseball/Base"]

    def test_price_override_keeps_currency(self):
        body = round_trip_offer_body(OFFER_RAW, price=3.99)
        assert body["pricingSummary"]["price"] == {"value": 3.99, "currency": "USD"}
        # The source offer is not mutated.
        assert OFFER_RAW["pricing_summary"]["price"]["value"] == "4.99"


class TestRefreshInventoryItemBody:
    def test_carries_the_item_across_with_live_pictures(self):
        item = InventoryItem.from_api(ITEM_RAW)
        body = refresh_inventory_item_body(item, ["gs://front", "gs://back"])
        assert body["product"]["title"] == "2026 Topps Card"
        assert body["product"]["description"] == "<p>item desc</p>"
        assert body["product"]["aspects"] == {
            "Player/Athlete": ["A Player"],
            "Features": ["Rookie", "SP"],
        }
        assert body["product"]["imageUrls"] == ["gs://front", "gs://back"]
        assert body["product"]["brand"] == "Topps"
        assert body["condition"] == "USED_VERY_GOOD"
        assert body["conditionDescription"] == "Light corner wear"
        assert body["availability"]["shipToLocationAvailability"]["quantity"] == 1


class FakeInventory:
    def __init__(self, offers=None):
        self.calls: list[tuple] = []
        self.offers = list(offers or [])

    def delete_inventory_item(self, sku):
        self.calls.append(("delete_inventory_item", sku))
        self.offers = []

    def upsert_inventory_item(self, sku, body):
        self.calls.append(("upsert_inventory_item", sku))

    def find_offer(self, sku):
        self.calls.append(("find_offer", sku))
        return self.offers[0] if self.offers else None

    def create_offer(self, body):
        self.calls.append(("create_offer", body["sku"]))
        return {"offerId": "222"}

    def update_offer(self, offer_id, body):
        self.calls.append(("update_offer", offer_id))

    def publish_offer(self, offer_id):
        self.calls.append(("publish_offer", offer_id))
        return {"listingId": "1000"}


class FakeMarketing:
    def __init__(self):
        self.calls: list[tuple] = []

    def delete_ad(self, *, campaign_id, ad_id):
        self.calls.append(("delete_ad", campaign_id, ad_id))

    def create_ads_by_inventory_reference(self, *, sku, rate, campaign_id):
        self.calls.append(("promote", sku, rate, campaign_id))


def fake_ebay(offers=None):
    return SimpleNamespace(inventory=FakeInventory(offers), marketing=FakeMarketing())


class TestRefreshListing:
    def test_deletes_item_then_recreates_and_promotes(self):
        ebay = fake_ebay([Offer.from_api(OFFER_RAW)])
        out = ListingService(ebay).refresh_listing(
            sku="SKU1",
            inventory_item_body={},
            offer_body={"sku": "SKU1"},
            existing_ad_id="AD",
            campaign_id="C",
            promote_rate=9,
            settle_seconds=0,
        )
        assert [c[0] for c in ebay.inventory.calls] == [
            "delete_inventory_item",
            "upsert_inventory_item",
            "find_offer",
            "create_offer",
            "publish_offer",
        ]
        assert ebay.inventory.calls[-1] == ("publish_offer", "222")
        assert ebay.marketing.calls == [("delete_ad", "C", "AD"), ("promote", "SKU1", 9, "C")]
        assert out["publish"] == {"listingId": "1000"}

    def test_not_promoted_without_a_campaign(self):
        ebay = fake_ebay()
        ListingService(ebay).refresh_listing(
            sku="SKU1", inventory_item_body={}, offer_body={"sku": "SKU1"}, settle_seconds=0
        )
        assert ebay.marketing.calls == []


class TestRecreateListing:
    def test_reuses_an_unpublished_offer_from_a_failed_attempt(self):
        unpublished = Offer.from_api({"offer_id": "333", "sku": "SKU1", "status": "UNPUBLISHED"})
        ebay = fake_ebay([unpublished])
        ListingService(ebay).recreate_listing(
            sku="SKU1", inventory_item_body={}, offer_body={"sku": "SKU1"}
        )
        names = [c[0] for c in ebay.inventory.calls]
        assert "create_offer" not in names
        assert ("update_offer", "333") in ebay.inventory.calls
        assert ebay.inventory.calls[-1] == ("publish_offer", "333")

    def test_leaves_an_already_published_offer_alone(self):
        ebay = fake_ebay([Offer.from_api(OFFER_RAW)])
        out = ListingService(ebay).recreate_listing(
            sku="SKU1", inventory_item_body={}, offer_body={"sku": "SKU1"}
        )
        assert [c[0] for c in ebay.inventory.calls] == ["upsert_inventory_item", "find_offer"]
        assert out["publish"] == {"listing_id": "999"}


class TestBuildSnapshot:
    def _ebay(self, item_raw=ITEM_RAW, offer_raw=OFFER_RAW):
        inventory = SimpleNamespace(
            get_inventory_item=lambda sku: InventoryItem.from_api(item_raw),
            find_offer=lambda sku: Offer.from_api(offer_raw) if offer_raw else None,
        )
        trading = SimpleNamespace(
            get_item_details=lambda item_id: {"picture_urls": ["https://i/front", "https://i/back"]}
        )
        return SimpleNamespace(inventory=inventory, trading=trading)

    def _row(self, item_id="999"):
        return pd.Series(
            {
                "sku": "SKU1",
                "item_id": item_id,
                "title": "t",
                "price": 4.99,
                "ad_id": float("nan"),
                "campaign_id": float("nan"),
                "bid_percentage": float("nan"),
            }
        )

    def test_preview_snapshot_uses_live_pictures(self):
        snap = refresh_listings.build_snapshot(
            self._row(), self._ebay(), None, settings=None, rehost=False
        )
        assert snap["inventory_item_body"]["product"]["imageUrls"] == [
            "https://i/front",
            "https://i/back",
        ]
        assert snap["offer_body"]["sku"] == "SKU1"
        assert snap["ad_id"] is None and snap["campaign_id"] is None
        assert snap["original"]["offer"] == OFFER_RAW

    def test_refuses_an_offer_not_live_on_this_listing(self):
        with pytest.raises(EbayClientError, match="not live"):
            refresh_listings.build_snapshot(
                self._row(item_id="555"), self._ebay(), None, settings=None, rehost=False
            )

    def test_refuses_variation_members(self):
        item = {**ITEM_RAW, "inventory_item_group_keys": ["G1"]}
        with pytest.raises(EbayClientError, match="variation"):
            refresh_listings.build_snapshot(
                self._row(), self._ebay(item_raw=item), None, settings=None, rehost=False
            )


def test_select_listings_needs_targets():
    with pytest.raises(ValueError):
        refresh_listings.select_listings(object())
