"""Assigning live listings to their planned store categories."""

import pandas as pd
import pytest

from shoebox.pipelines.assign_store_categories import build_assignments
from shoebox.transforms.listing_builder import offer_body_with_store_categories


def _plan() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "item_id": "1",
                "sku": "A",
                "title": "a baseball card",
                "sport": "Baseball",
                "is_variation": "False",
                "primary_category": "/Baseball Singles/Chicago Cubs",
                "secondary_category": "/A_Hits/Numbered",
            },
            {
                "item_id": "2",
                "sku": "",
                "title": "a sku-less baseball card",
                "sport": "Baseball",
                "is_variation": "False",
                "primary_category": "/Baseball Singles/New York Mets",
                "secondary_category": "",
            },
            {
                "item_id": "3",
                "sku": "C",
                "title": "a basketball card",
                "sport": "Basketball",
                "is_variation": "False",
                "primary_category": "/Basketball Singles",
                "secondary_category": "",
            },
            {
                "item_id": "4",
                "sku": "D",
                "title": "a you pick listing",
                "sport": "Baseball",
                "is_variation": "True",
                "primary_category": "/Complete Your Set - You Pick",
                "secondary_category": "",
            },
        ]
    )


class TestBuildAssignments:
    def test_defaults_to_baseball_only(self):
        out = build_assignments(_plan())
        assert set(out["item_id"]) == {"1", "2"}

    def test_variation_listings_are_left_alone(self):
        # They are already in the You Pick branch and have no team.
        out = build_assignments(_plan(), sport=None)
        assert "4" not in set(out["item_id"])

    def test_other_sports_can_be_targeted(self):
        out = build_assignments(_plan(), sport="Basketball")
        assert list(out["item_id"]) == ["3"]

    def test_route_follows_whether_the_listing_has_a_sku(self):
        out = build_assignments(_plan()).set_index("item_id")
        assert out.loc["1", "update_method"] == "inventory"
        assert out.loc["2", "update_method"] == "trading"

    def test_hits_are_left_blank_unless_asked_for(self):
        # Blank means "carry across whatever the listing already has", so a
        # hand-set Hits category is not clobbered.
        out = build_assignments(_plan()).set_index("item_id")
        assert out.loc["1", "secondary_category"] == ""
        assert out.loc["1", "planned_secondary"] == "/A_Hits/Numbered"

    def test_with_hits_pushes_the_planned_category(self):
        out = build_assignments(_plan(), with_hits=True).set_index("item_id")
        assert out.loc["1", "secondary_category"] == "/A_Hits/Numbered"


class TestOfferBody:
    @staticmethod
    def _offer() -> dict:
        return {
            "sku": "A",
            "offer_id": "9",
            "marketplace_id": "EBAY_US",
            "format": "FIXED_PRICE",
            "available_quantity": 1,
            "category_id": "261328",
            "listing_description": "<p>a card</p>",
            "listing_duration": "GTC",
            "merchant_location_key": "SFTahoeCards",
            "listing_policies": {
                "fulfillment_policy_id": "111",
                "payment_policy_id": "222",
                "return_policy_id": "333",
                "best_offer_terms": {"best_offer_enabled": True},
            },
            "pricing_summary": {"price": {"value": "1.29", "currency": "USD"}},
            "store_category_names": ["/Parallels"],
        }

    def test_only_the_categories_change(self):
        body = offer_body_with_store_categories(self._offer(), ["/Baseball Singles/Chicago Cubs"])
        assert body["storeCategoryNames"] == ["/Baseball Singles/Chicago Cubs"]
        assert body["pricingSummary"]["price"]["value"] == "1.29"
        assert body["availableQuantity"] == 1
        assert body["listingDescription"] == "<p>a card</p>"

    def test_policies_are_carried_across_not_recomputed(self):
        # Re-deriving fulfillment from price would reshuffle shipping on any
        # listing whose price drifted past the threshold since it was created.
        body = offer_body_with_store_categories(self._offer(), ["/Baseball Singles/Chicago Cubs"])
        assert body["listingPolicies"]["fulfillmentPolicyId"] == "111"
        assert body["listingPolicies"]["paymentPolicyId"] == "222"
        assert body["listingPolicies"]["returnPolicyId"] == "333"
        assert body["listingPolicies"]["bestOfferTerms"] == {"bestOfferEnabled": True}

    def test_two_categories_are_carried(self):
        body = offer_body_with_store_categories(
            self._offer(), ["/Baseball Singles/Chicago Cubs", "/A_Hits/Numbered"]
        )
        assert body["storeCategoryNames"] == [
            "/Baseball Singles/Chicago Cubs",
            "/A_Hits/Numbered",
        ]

    def test_missing_optional_fields_are_dropped_not_nulled(self):
        offer = self._offer()
        del offer["listing_description"]
        body = offer_body_with_store_categories(offer, ["/Baseball Singles/Chicago Cubs"])
        assert "listingDescription" not in body


class TestClientGuards:
    def test_categories_are_required(self):
        from shoebox.services.listings import ListingService

        with pytest.raises(ValueError, match="categories must not be empty"):
            ListingService(ebay=object()).update_store_categories(categories=[], sku="A")

    def test_a_sku_or_item_id_is_required(self):
        from shoebox.services.listings import ListingService

        with pytest.raises(ValueError, match="needs a sku or an item_id"):
            ListingService(ebay=object()).update_store_categories(categories=["/x"])

    def test_trading_route_needs_resolved_ids(self):
        from shoebox.services.listings import ListingService

        with pytest.raises(ValueError, match="Trading needs category_ids"):
            ListingService(ebay=object()).update_store_categories(categories=["/x"], item_id="1")
